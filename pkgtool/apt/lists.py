"""pkgtool.apt.lists — /var/lib/apt/lists 的唯一解析器。

合并原先两套并存的解析：deb_backend.build_source_index()（来源判定用）和
apt_repo.load_index()（搜索/升级检测用）。二者读的是同一批 Packages 文件，
每次采集要解压解析两遍，且字段各取一半、语义还不完全一致。

一次遍历同时产出：
  · (包名, 版本) → 源标签集合      来源判定：包是否来自某个已配置源
  · 包名 → 全部版本条目            搜索、版本列表、升级候选
  · 包名 → section / priority      卸载分类（classify.py）用
源标签 = Release 文件的 Label（缺失时用 Origin，再缺失用文件名前缀）
        + 文件名里解析出的 suite / component，形如 "Ubuntu noble/main"。

无需 root：lists 目录全局可读。
"""
import glob
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from functools import cmp_to_key

from ..base import matches_tokens, query_tokens
from ..compress import open_text, read_text, strip_compressed_suffix
from ..config import CFG
from .version import newest, ver_cmp, ver_gt

# Packages 记录里需要留存的字段（其余忽略）
_FIELDS = frozenset(("Package", "Version", "Architecture", "Maintainer", "Size",
                     "Filename", "Description", "Section", "Priority"))
_PGP_TAIL = "-----BEGIN PGP SIGNATURE-----"
_PGP_HEAD = "-----BEGIN PGP SIGNED MESSAGE-----"


@dataclass(slots=True)
class RepoEntry:
    """某源里某个包的一个版本。"""
    version: str
    repo: str
    suite: str
    component: str
    arch: str
    maintainer: str
    size_kb: int
    filename: str
    date: str
    desc: str
    section: str
    priority: str


@dataclass(slots=True)
class _Release:
    label: str
    suite: str
    date: str


def _iter_records(lines):
    """流式解析 Debian 控制文件：空行分隔记录；续行（空格/制表符开头）跳过，
    因此 Description 只留首行摘要，不把多行正文吃进内存。"""
    cur = {}
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            if cur:
                yield cur
                cur = {}
            continue
        if line[0] in " \t":
            continue
        key, sep, value = line.partition(": ")
        if sep and key in _FIELDS and key not in cur:
            cur[key] = value.strip()
    if cur:
        yield cur


def _release_body(path):
    """InRelease 的字段在 PGP 头之后的第一个空行后；Release 文件直接就是字段。"""
    text = read_text(path)
    if text is None:
        return ""
    if text.startswith(_PGP_HEAD):
        _head, _sep, body = text.partition("\n\n")
        text = body
    end = text.find(_PGP_TAIL)
    return text if end < 0 else text[:end]


def _parse_releases(lists_dir):
    """→ {文件名前缀: _Release}。"""
    out = {}
    for path in glob.glob(os.path.join(lists_dir, "*")):
        base = os.path.basename(path)
        if base.endswith(".gpg"):
            continue
        prefix = None
        for suffix in ("_InRelease", "_Release"):
            if base.endswith(suffix):
                prefix = base[: -len(suffix)]
                break
        if prefix is None:
            continue
        label = origin = suite = date = ""
        for line in _release_body(path).splitlines():
            key, sep, value = line.partition(": ")
            if not sep:
                continue
            value = value.strip()
            if key == "Label" and not label:
                label = value
            elif key == "Origin" and not origin:
                origin = value
            elif key == "Suite" and not suite:
                suite = value
            elif key == "Date" and not date:
                try:
                    date = parsedate_to_datetime(value).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    date = value
        # Label 优先于 Origin（apt 自己也是这么显示的）：Release 里 Origin 常写在
        # Label 前面，按出现顺序取会把 "NVIDIA CUDA" 丢成 "NVIDIA"。
        out[prefix] = _Release(label or origin or prefix, suite, date)
    return out


def _suite_component(basename, cfg):
    """从 Packages 文件名解析 suite/component：
      规范仓库  <prefix>_dists_<suite>_<component>_binary-<arch>_Packages[.gz]
      扁平仓库（sources.list 里写 `deb <url> ./`）文件名里没有 _dists_，
      硬套位置会把 URL 路径段当成 suite——NVIDIA 的 CUDA 源曾被解析成
      suite="x86%5f64"，所以这种情况直接返回空，只留 Release 里的 Label。"""
    core = strip_compressed_suffix(basename).split("_binary-")[0]
    if "_dists_" not in core:
        return "", ""
    toks = core.split("_")
    comp = toks[-1] if toks[-1] in cfg.known_components else ""
    suite = ""
    if len(toks) >= 2 and (comp or toks[-2] not in ("dists", "deb")):
        suite = toks[-2]
    return suite, comp


def _package_files(lists_dir, cfg):
    """→ [(路径, 文件名前缀, suite, component)]，已按架构过滤。"""
    skip = re.compile(cfg.index_skip_arch_re)
    out = []
    for path in sorted(set(glob.glob(os.path.join(lists_dir, "*_Packages"))
                           + glob.glob(os.path.join(lists_dir, "*_Packages.*")))):
        base = os.path.basename(path)
        if base.endswith((".gpg", ".tmp")) or skip.search(base):
            continue
        core = strip_compressed_suffix(base)
        if not core.endswith("_Packages"):
            continue
        suite, comp = _suite_component(base, cfg)
        out.append((path, core[: -len("_Packages")], suite, comp))
    return out


def _match_release(prefix, releases):
    """Packages 文件名比 Release 多 component/arch 段，取最长前缀匹配。"""
    best = None
    for r in releases:
        if prefix == r or prefix.startswith(r + "_"):
            if best is None or len(r) > len(best):
                best = r
    return releases.get(best)


class RepoIndex:
    """已解析的仓库索引，构造后只读。"""

    def __init__(self, entries):
        self._entries = entries          # name → [RepoEntry]
        self._top = {}                   # name → 最新版本的 RepoEntry（惰性缓存）

    def __len__(self):
        return len(self._entries)

    def __contains__(self, name):
        return name in self._entries

    def names(self):
        return self._entries.keys()

    def entries(self, name):
        return self._entries.get(name, [])

    def top(self, name):
        """最新版本的条目（同版本多源时取先出现的）；无此包返回 None。"""
        if name not in self._top:
            es = self._entries.get(name) or []
            if not es:
                self._top[name] = None
            else:
                best = newest(e.version for e in es)
                self._top[name] = next(e for e in es if e.version == best)
        return self._top[name]

    def candidate(self, name):
        """仓库里的最高版本；包不在任何源则返回 ""。"""
        e = self.top(name)
        return e.version if e else ""

    def versions(self, name):
        """去重后的版本列表，按 Debian 版本序从高到低。"""
        vs = {e.version for e in self.entries(name)}
        return sorted(vs, key=cmp_to_key(ver_cmp), reverse=True)

    def repos(self, name, version=None):
        """命中的源标签：给了 version 就按 (名,版本) 精确匹配，否则按包名。"""
        es = self.entries(name)
        if version is not None:
            es = [e for e in es if e.version == version]
        out = []
        for e in es:
            if e.repo not in out:
                out.append(e.repo)
        return out

    def meta(self, name):
        """→ {section, priority}（取最新版本条目），供卸载分类用。"""
        e = self.top(name)
        return {"section": e.section, "priority": e.priority} if e else {}

    def search(self, q, limit=30):
        """按包名/描述搜索。
        排名：完全匹配 > 前缀 > 名字含 > 描述含 > 分词全命中。最后一级用分词，
        让 "zen browser" 这类多词查询也能命中名字里用下划线/连字符的包。
        先全量扫描再排序——原实现边扫边在 limit*3 处 break，结果取决于 dict
        插入顺序，会漏掉更优匹配。"""
        q = (q or "").lower().strip()
        if not q:
            return []
        tokens = query_tokens(q)
        hits = []
        for name in self._entries:
            nl = name.lower()
            e = self.top(name)
            desc = ((e.desc if e else "") or "").lower()
            if nl == q:
                rank = 0
            elif nl.startswith(q):
                rank = 1
            elif q in nl:
                rank = 2
            elif q in desc:
                rank = 3
            elif matches_tokens(f"{name} {desc}", tokens):
                rank = 4
            else:
                continue
            hits.append((rank, name))
        hits.sort()
        out = []
        for _rank, name in hits[:limit]:
            e = self.top(name)
            out.append({"name": name, "version": e.version, "repo": e.repo,
                        "suite": e.suite, "component": e.component,
                        "maintainer": e.maintainer, "size_kb": e.size_kb,
                        "date": e.date, "desc": e.desc, "section": e.section})
        return out

    def info(self, name):
        """单包详情：全部版本（每源每版本一条）。"""
        es = self.entries(name)
        if not es:
            return None
        # RepoEntry 是 slots dataclass，vars() 会抛 TypeError，得用 asdict
        return {"name": name, "candidate": self.candidate(name),
                "versions": [asdict(e) for e in es]}


_CACHE = {"t": 0.0, "mtime": None, "idx": None}
_LOCK = threading.Lock()


def _lists_mtime(lists_dir):
    """目录 mtime：apt update 会重写列表文件，比单纯 TTL 更准。"""
    try:
        return os.path.getmtime(lists_dir)
    except OSError:
        return None


def load_index(cfg=CFG, force=False):
    """解析并缓存仓库索引；force=True 忽略缓存。"""
    with _LOCK:
        now = time.time()
        mtime = _lists_mtime(cfg.apt_lists_dir)
        if (_CACHE["idx"] is not None and not force
                and now - _CACHE["t"] < cfg.index_cache_ttl
                and _CACHE["mtime"] == mtime):
            return _CACHE["idx"]
        idx = _build(cfg)
        _CACHE.update(t=now, mtime=mtime, idx=idx)
        return idx


def _build(cfg):
    releases = _parse_releases(cfg.apt_lists_dir)
    entries = {}
    for path, prefix, suite, comp in _package_files(cfg.apt_lists_dir, cfg):
        rel = _match_release(prefix, releases)
        label = rel.label if rel else prefix
        loc = "/".join(x for x in (suite or (rel.suite if rel else ""), comp) if x)
        tag = f"{label} {loc}".strip()
        date = rel.date if rel else ""
        try:
            with open_text(path) as fh:
                for rec in _iter_records(fh):
                    name, ver = rec.get("Package"), rec.get("Version")
                    arch = rec.get("Architecture", "")
                    if not name or not ver or arch not in cfg.index_arches:
                        continue
                    size = rec.get("Size", "")
                    entries.setdefault(name, []).append(RepoEntry(
                        version=ver, repo=tag, suite=suite, component=comp,
                        arch=arch, maintainer=rec.get("Maintainer", "?"),
                        size_kb=int(size) // 1024 if size.isdigit() else 0,
                        filename=rec.get("Filename", ""), date=date,
                        desc=rec.get("Description", "")[:160],
                        section=rec.get("Section", ""),
                        priority=rec.get("Priority", "")))
        except OSError:
            continue
    return RepoIndex(entries)


def is_upgrade(candidate, installed):
    """仓库候选版本是否高于已安装版本。"""
    return bool(candidate) and ver_gt(candidate, installed)
