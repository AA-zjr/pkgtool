"""pkgtool.catalog — 跨格式远程目录搜索与安装（apt / snap / flatpak）。

三个来源的取数方式差别很大，这里统一成 CatalogItem：

  apt      读本地 /var/lib/apt/lists 索引。离线、毫秒级、含描述与全部候选版本。
  snap     `snap find`，联网查 Snap Store，约 2 秒，含发布者/注记/摘要。
  flatpak  `flatpak remote-ls`，读本地 remote 的 refs 缓存，0.3 秒 / 3483 条，
           能拿到安装体积，但**没有描述**——描述在 appstream 目录缓存里。

flatpak 为什么不用 `flatpak search`：它依赖 remote 的 appstream 缓存，本机
没有时会去联网拉取，实测挂住 25 秒且无任何输出。remote-ls 走 refs 缓存，
稳定可用，代价是搜不到摘要、只能按名字和 app-id 匹配。

各源结果都缓存：flatpak 缓存全量清单（之后每次搜索只是本地过滤，输入即搜），
snap 按查询词缓存（它必须联网，不能每敲一个字符就打一次商店）。
"""
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .apt import actions, lists
from .base import is_safe_name
from .config import CFG

SOURCES = ("apt", "snap", "flatpak")

_SNAP_SPLIT_RE = re.compile(r"\s{2,}")
_FLATPAK_COLUMNS = "name,application,installed,branch"
_C_ENV = {"LC_ALL": "C", "LANG": "C"}      # 固定表头语言，解析才稳

_LOCK = threading.Lock()
_FLATPAK_CACHE = {"t": 0.0, "items": None}
_SNAP_CACHE = {}

# 最近一次 search() 的每源耗时与失败信息，供界面提示"哪个源慢/挂了"
LAST_ELAPSED = {}
LAST_ERRORS = []


@dataclass(slots=True)
class CatalogItem:
    """一条可安装条目（跨源统一结构）。"""
    source: str                 # apt / snap / flatpak
    name: str                   # 安装标识：apt 包名 / snap 名 / flatpak app-id
    display: str = ""           # 显示名（flatpak 有独立的 name 字段）
    version: str = ""
    summary: str = ""
    publisher: str = ""         # apt 维护者 / snap 发布者 / flatpak remote
    size_text: str = ""
    channel: str = ""           # apt 的 suite/component、snap 的注记、flatpak 的 branch
    remote: str = ""            # flatpak 的 remote 名
    classic: bool = False       # snap 经典 confinement，安装时必须带 --classic
    installed: bool = False     # 本机是否已装
    extra: dict = field(default_factory=dict)

    @property
    def title(self):
        return self.display or self.name


def _run(argv, cfg, timeout=None):
    """跑一个查询命令，返回 stdout 行列表；任何失败都返回 []（搜索不该抛异常）。"""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout or cfg.timeout_catalog,
                           env=dict(os.environ, **_C_ENV))
    except (OSError, subprocess.SubprocessError):
        return []
    return p.stdout.splitlines() if p.returncode == 0 else []


# ---------- 各来源 ----------


def _search_apt(q, limit, cfg):
    out = []
    for x in lists.load_index(cfg).search(q, limit):
        out.append(CatalogItem(
            source="apt", name=x["name"], display=x["name"], version=x["version"],
            summary=x.get("desc") or "", publisher=x.get("maintainer") or "",
            size_text=f"{x['size_kb']}K" if x.get("size_kb") else "",
            channel=" ".join(y for y in (x.get("suite"), x.get("component")) if y),
            remote=x.get("repo") or "",
            extra={"section": x.get("section", "")}))
    return out


def _search_snap(q, limit, cfg):
    """`snap find` 按 2+ 空格分列：Name Version Publisher Notes Summary。
    表头随 locale 变，所以统一用 LC_ALL=C 固定成英文再跳过首行。"""
    now = time.time()
    with _LOCK:
        hit = _SNAP_CACHE.get(q)
        if hit and now - hit[0] < cfg.catalog_cache_ttl:
            return hit[1][:limit]
    lines = _run(["snap", "find", q], cfg)
    out = []
    for i, line in enumerate(lines):
        f = _SNAP_SPLIT_RE.split(line.strip())
        if len(f) < 5:
            continue
        if i == 0 and f[0] == "Name":
            continue                                  # 表头
        notes = f[3]
        out.append(CatalogItem(source="snap", name=f[0], display=f[0],
                               version=f[1], publisher=f[2], channel=notes,
                               summary=" ".join(f[4:]),
                               classic="classic" in notes.lower()))
    with _LOCK:
        _SNAP_CACHE[q] = (now, out)
    return out[:limit]


def flatpak_remotes(cfg):
    """已配置的 flatpak remote 名（install 时需要指明从哪个 remote 装）。"""
    return [l.split("\t")[0].strip()
            for l in _run(["flatpak", "remotes", "--columns=name"], cfg)
            if l.strip()]


def _flatpak_catalog(cfg):
    """全部 remote 的应用清单（缓存）。→ [CatalogItem]"""
    now = time.time()
    with _LOCK:
        if _FLATPAK_CACHE["items"] is not None \
                and now - _FLATPAK_CACHE["t"] < cfg.catalog_cache_ttl:
            return _FLATPAK_CACHE["items"]
    out = []
    for remote in flatpak_remotes(cfg):
        for line in _run(["flatpak", "remote-ls", "--app",
                          f"--columns={_FLATPAK_COLUMNS}", remote], cfg):
            f = line.split("\t")
            if len(f) < 4 or not f[1]:
                continue
            # flatpak 的体积列用不间断空格分隔（"518.3\xa0MB"），不换成普通
            # 空格会打乱终端对齐、也没法当普通字符串比较
            size = f[2].replace("\xa0", " ").strip()
            out.append(CatalogItem(source="flatpak", name=f[1], display=f[0],
                                   size_text=size, channel=f[3].strip(),
                                   publisher=remote, remote=remote))
    with _LOCK:
        _FLATPAK_CACHE.update(t=now, items=out)
    return out


def _rank(items, q, limit):
    """本地过滤 + 排名：完全匹配 > 前缀 > 名字含 > app-id 含。"""
    ql = q.lower()
    hits = []
    for it in items:
        n, d = it.name.lower(), it.display.lower()
        if ql in (n, d):
            r = 0
        elif n.startswith(ql) or d.startswith(ql):
            r = 1
        elif ql in n or ql in d:
            r = 2
        else:
            continue
        hits.append((r, it.display or it.name, it))
    hits.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in hits[:limit]]


def _search_flatpak(q, limit, cfg):
    return _rank(_flatpak_catalog(cfg), q, limit)


_SEARCHERS = {"apt": _search_apt, "snap": _search_snap, "flatpak": _search_flatpak}
# 搜索来源名 → PackageRecord.pkg_type（apt 对应的是 deb）
_SOURCE_TYPE = {"apt": "deb", "snap": "snap", "flatpak": "flatpak"}


def available(cfg=CFG):
    """本机可用的搜索来源。"""
    out = []
    if os.path.isdir(cfg.apt_lists_dir):
        out.append("apt")
    if shutil.which("snap") and os.path.isdir(cfg.snap_store_dir):
        out.append("snap")
    if shutil.which("flatpak"):
        out.append("flatpak")
    return tuple(out)


def search(query, sources=None, limit=30, cfg=CFG, installed=None):
    """跨源搜索 → [CatalogItem]，按来源分组、组内按相关度排。
    sources=None 表示本机全部可用来源；每个来源各自取 limit 条，
    免得一个源的结果把其他源挤掉。
    installed 是 {(source, name)} 集合，用来标注"已安装"。"""
    q = (query or "").strip()
    if not q:
        return []
    sources = tuple(s for s in (sources or available(cfg)) if s in _SEARCHERS)
    installed = installed or set()
    out = []
    LAST_ERRORS.clear()
    LAST_ELAPSED.clear()
    for s in sources:
        pkg_type = _SOURCE_TYPE[s]
        t0 = time.time()
        try:
            found = _SEARCHERS[s](q, limit, cfg)
        except Exception as e:                        # noqa: BLE001 单源失败不拖垮整体
            found = []
            LAST_ERRORS.append(f"{s}: {type(e).__name__}: {e}")
        LAST_ELAPSED[s] = round(time.time() - t0, 1)
        for it in found:
            it.installed = (pkg_type, it.name) in installed
        out.extend(found)
    return out


def installed_keys(inv):
    """从 Inventory 提取 {(pkg_type, name)}，供 search 标注已安装。"""
    return {(r.pkg_type, r.name) for r in inv.records}


def installed_names(cfg=CFG, sources=SOURCES):
    """→ {(pkg_type, name)}，只跑相关的后端，不做全量盘点。
    搜索不该为了标注"已安装"而等一次 4 秒的全量采集：deb 读 status 只要
    0.01 秒，snap / flatpak 各自也就 0.1 秒。"""
    out = set()
    if "apt" in sources:
        from .apt import dpkg
        out |= {("deb", n) for n in dpkg.installed(cfg)}
    if "snap" in sources:
        from .backends.snap import SnapBackend
        if SnapBackend.available(cfg):
            out |= {("snap", r.name) for r in SnapBackend(cfg).collect()}
    if "flatpak" in sources:
        from .backends.flatpak import FlatpakBackend
        if FlatpakBackend.available(cfg):
            out |= {("flatpak", r.name) for r in FlatpakBackend(cfg).collect()}
    return out


# ---------- 安装 ----------


def install_argv(item, version=""):
    """→ (argv, privileged)。名称先过白名单，拒绝参数注入。"""
    if not is_safe_name(item.name):
        return None, f"名称非法，拒绝安装：{item.name!r}"
    if item.source == "apt":
        spec = f"{item.name}={version}" if version else item.name
        if version and not is_safe_name(version):
            return None, f"版本非法：{version!r}"
        return ["apt-get", "install", "-y", spec], True
    if item.source == "snap":
        argv = ["snap", "install"]
        if item.classic:
            argv.append("--classic")   # 经典 confinement 不带这个标志会直接失败
        return argv + [item.name], True
    if item.source == "flatpak":
        argv = ["flatpak", "install", "-y"]
        if item.remote and is_safe_name(item.remote):
            argv.append(item.remote)
        return argv + [item.name], True
    return None, f"不支持安装的来源：{item.source}"


def install(item, version="", on_line=None, cfg=CFG):
    """安装一条目录项 → actions.Result。"""
    argv, privileged = install_argv(item, version)
    if argv is None:
        return actions.Result(ok=False, error=privileged)   # 此时第二项是错误信息
    if privileged:
        return actions.run_privileged(argv, timeout=cfg.timeout_upgrade,
                                      on_line=on_line, cfg=cfg)
    return actions.run_plain(argv, timeout=cfg.timeout_upgrade, cfg=cfg)


def describe(item, cfg=CFG):
    """单条详情文本。apt 给全部候选版本，snap 给 `snap info`，flatpak 给基本字段。"""
    if item.source == "apt":
        from . import report
        info = lists.load_index(cfg).info(item.name)
        return report.render_versions(info) if info else "仓库中无此包"
    if item.source == "snap":
        lines = _run(["snap", "info", item.name], cfg)
        return "\n".join(lines) if lines else "（snap info 无输出）"
    bits = [("app-id", item.name), ("显示名", item.display),
            ("remote", item.remote), ("branch", item.channel),
            ("体积", item.size_text)]
    return "\n".join(f"  {k:<10}{v}" for k, v in bits if v)
