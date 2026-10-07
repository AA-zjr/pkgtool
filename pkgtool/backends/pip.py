"""pkgtool.backends.pip — 识别所有 Python 环境里的 pip 包。

纯文件系统解析，不执行目标 python（执行陌生解释器有副作用，且慢）：
每个 *.dist-info 目录 = 一个 pip 包，版本取目录名，Name/Summary 读 METADATA，
CLI 命令解析 entry_points.txt 的 [console_scripts]。

环境发现四路：
  1. conda/anaconda/miniforge：base + envs/*，以及 `conda info --envs` 给出的
     前缀式环境（conda create -p）
  2. 兜底扫描：conda-meta 标记（能发现未注册/二进制不可见的前缀环境）
  3. venv：pyvenv.cfg（含隐藏目录；常规 walk 会剪掉 .venv，所以单独一轮）
  4. user-site（~/.local）与系统 pip（/usr/local/lib/python3*/dist-packages）
site-packages 路径按 realpath 跨来源去重，避免 python3.1 前缀匹配到 python3.12
造成的重复计数。
"""
import glob
import os
import re
import subprocess
import threading
import time

from ..base import BYTES_PER_MB, Backend, OriginKind, PackageRecord
from ..config import CFG

_DIST_INFO_RE = re.compile(
    r"^(?P<name>.+)-(?P<ver>[A-Za-z0-9](?:[A-Za-z0-9.]*[A-Za-z0-9])?)$")
_ENV_CACHE = {"t": 0.0, "envs": None, "empties": []}
_LOCK = threading.Lock()


def _site_dirs(root):
    """一个 python 环境根下的 site-packages/dist-packages（realpath 去重）。"""
    out, seen = [], set()
    for sub in ("site-packages", "dist-packages"):
        for d in glob.glob(os.path.join(root, "lib", "python*", sub)):
            if not os.path.isdir(d):
                continue
            try:
                rp = os.path.realpath(d)
            except OSError:
                continue
            if rp not in seen:
                seen.add(rp)
                out.append(d)
    return out


def conda_dists(cfg):
    """常见位置的 conda 发行版根（home、/opt、/usr/local）。
    conda-meta 目录存在才算真根，避免把同名普通目录当成发行版。
    clean 子命令也用它定位 conda 包缓存。"""
    out = []
    bases = (cfg.home, cfg.python_system_root) + tuple(cfg.conda_scan_roots)
    for base in bases:
        for name in cfg.conda_dir_names:
            p = os.path.join(base, name)
            if os.path.isdir(os.path.join(p, "conda-meta")):
                out.append(p)
    return out


def _conda_info_envs(dist, cfg):
    """`conda info --envs` → [环境路径]。权威列表，含前缀式环境。"""
    binary = os.path.join(dist, "bin", "conda")
    if not os.path.isfile(binary):
        return []
    try:
        p = subprocess.run([binary, "info", "--envs"], capture_output=True,
                           text=True, timeout=cfg.timeout_query)
    except (OSError, subprocess.SubprocessError):
        return []
    paths = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("*", " ").replace("+", " ").split()
        if len(parts) >= 2 and os.path.isdir(parts[-1]):
            paths.append(os.path.realpath(parts[-1]))
    return paths


def _rel_or_abs(p, cfg):
    """主目录内的路径显示成相对形式（更短更好读），其余用绝对路径。"""
    r = os.path.relpath(p, cfg.home)
    return p if r.startswith("..") else r


def _walk_markers(cfg):
    """一轮扫描找两种标记，发现命令行工具没注册的环境：
      conda-meta   前缀式 conda 环境（记录后继续深入）
      pyvenv.cfg   venv（记录后不再深入，venv 里套 venv 没有意义）
    """
    found = []
    for base in cfg.python_scan_roots:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath[len(base):].count(os.sep)
            if "pyvenv.cfg" in filenames:
                found.append((_rel_or_abs(dirpath, cfg), dirpath))
                dirnames[:] = []
                continue
            if "conda-meta" in dirnames:
                found.append((_rel_or_abs(dirpath, cfg), dirpath))
                dirnames.remove("conda-meta")
            dirnames[:] = [d for d in dirnames if d not in cfg.pip_prune_dirs]
            if depth >= cfg.python_scan_maxdepth:
                dirnames[:] = []
    return found


def find_python_envs(cfg=CFG):
    """→ [(label, [site_packages_dir, ...])]，label 如 miniconda3/Pytorch、agent/.venv。
    结果缓存 env_cache_ttl 秒（一次盘点里 collect 和 environment_summary 各调一次）。"""
    with _LOCK:
        now = time.time()
        if _ENV_CACHE["envs"] is not None and now - _ENV_CACHE["t"] < cfg.env_cache_ttl:
            return _ENV_CACHE["envs"]

        roots, seen_roots = [], set()

        def add_root(label, root):
            try:
                rp = os.path.realpath(root)
            except OSError:
                return
            if rp not in seen_roots:
                seen_roots.add(rp)
                roots.append((label, root))

        # 1) conda 发行版 × conda info 权威环境列表（含前缀式）
        for dist in conda_dists(cfg):
            dname = os.path.basename(dist)
            drp = os.path.realpath(dist)
            envs_dir = os.path.realpath(os.path.join(dist, "envs"))
            listed = _conda_info_envs(dist, cfg)
            if not listed:            # conda 二进制不可用时的兜底：base + envs/*
                listed = [drp]
                if os.path.isdir(envs_dir):
                    listed += [os.path.realpath(os.path.join(envs_dir, e))
                               for e in sorted(os.listdir(envs_dir))
                               if os.path.isdir(os.path.join(envs_dir, e))]
            for ep in listed:
                if ep == drp:
                    add_root(f"{dname}/base", ep)
                elif ep.startswith(envs_dir + os.sep):
                    add_root(f"{dname}/{os.path.basename(ep)}", ep)
                else:                 # 前缀式环境（conda create -p）
                    add_root(_rel_or_abs(ep, cfg), ep)

        # 2) + 3) 标记扫描：conda-meta 与 pyvenv.cfg
        for label, root in _walk_markers(cfg):
            add_root(label, root)

        # 4) user-site 与系统 pip
        add_root("user-site", os.path.join(cfg.home, ".local"))
        add_root("system", cfg.python_system_root)

        envs, seen_sp, empties = [], set(), []
        for label, root in roots:
            keep = []
            for d in _site_dirs(root):
                rp = os.path.realpath(d)
                if rp not in seen_sp:   # 跨来源去重：同一 site-packages 只算一次
                    seen_sp.add(rp)
                    keep.append(d)
            if keep:
                envs.append((label, keep))
            elif label not in ("user-site", "system"):
                empties.append(label)   # 真环境但没装 python = 空壳，也报出来
        _ENV_CACHE.update(t=now, envs=envs, empties=empties)
        return envs


def environment_summary(cfg=CFG):
    """全部探测到的 Python 环境（含没有 python 的空壳）→ [{label, packages, empty?}]。"""
    out = []
    for label, sp_dirs in find_python_envs(cfg):
        n = sum(len(glob.glob(os.path.join(sp, "*.dist-info"))) for sp in sp_dirs)
        if n == 0 and label in ("user-site", "system"):
            continue                    # 空伪根不占位
        out.append({"label": label, "packages": n})
    for label in _ENV_CACHE.get("empties") or []:
        out.append({"label": label, "packages": 0, "empty": True})
    return out


def _parse_dist_info(di):
    """解析一个 dist-info 目录 → (name, version, summary, [console_scripts])。"""
    base = os.path.basename(di)[: -len(".dist-info")]
    m = _DIST_INFO_RE.match(base)
    name, ver = (m.group("name"), m.group("ver")) if m else (base, "?")
    summary = ""
    try:
        with open(os.path.join(di, "METADATA"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Summary: "):
                    summary = line[9:].strip()[:100]
                elif not line.strip():
                    break               # header 结束
    except OSError:
        pass
    exes = []
    try:
        with open(os.path.join(di, "entry_points.txt"),
                  encoding="utf-8", errors="replace") as fh:
            in_cs = False
            for line in fh:
                line = line.strip()
                if line == "[console_scripts]":
                    in_cs = True
                elif line.startswith("["):
                    in_cs = False
                elif in_cs and "=" in line:
                    exes.append(line.split("=", 1)[0].strip())
    except OSError:
        pass
    return name, ver, summary, exes


def _record_size(di):
    """从 dist-info/RECORD 读精确字节数（第三列），比遍历目录快也更准。
    RECORD 路径可能指向 site-packages 之外（../../../bin/pip），但体积照算。
    读不到就退回 0，不猜。"""
    total = 0
    try:
        with open(os.path.join(di, "RECORD"), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").rsplit(",", 2)
                if len(parts) == 3 and parts[2].strip().isdigit():
                    total += int(parts[2])
    except OSError:
        return 0.0
    return round(total / BYTES_PER_MB, 1)


class PipBackend(Backend):
    pkg_type = "pip"

    @classmethod
    def available(cls, cfg=CFG):
        cands = [os.path.join(cfg.home, d) for d in cfg.conda_dir_names]
        cands += glob.glob(os.path.join(cfg.home, ".local", "lib", "python*"))
        cands += glob.glob(os.path.join(cfg.python_system_root, "lib", "python3*"))
        return any(os.path.isdir(c) for c in cands)

    def collect(self):
        records = []
        for label, sp_dirs in find_python_envs(self.cfg):
            for sp in sp_dirs:
                for di in sorted(glob.glob(os.path.join(sp, "*.dist-info"))):
                    name, ver, summary, exes = _parse_dist_info(di)
                    try:
                        mtime = time.strftime("%Y-%m-%d",
                                              time.localtime(os.path.getmtime(di)))
                    except OSError:
                        mtime = ""
                    records.append(PackageRecord(
                        pkg_type=self.pkg_type, name=name, version=ver,
                        # 同一个包名会出现在多个环境里（numpy 在 base/Pytorch/.venv
                        # 各一份），不带环境维度它们会撞 key 被去重掉
                        variant=label,
                        origin_kind=OriginKind.REPO,
                        origin_repos=[f"pypi({label})"],
                        size_mb=_record_size(di),
                        install_path=sp,
                        executables=exes[:self.cfg.exec_list_limit],
                        first_install=mtime,
                        extra={"env": label, "site_packages": sp,
                               "summary": summary, "commands": exes}))
        return records
