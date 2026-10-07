"""pkgtool.pip_backend — 识别所有 Python 环境里的 pip 包（纯文件系统解析，不执行 python）。

环境发现：
  1. conda/anaconda/miniforge: base + envs/*（lib/python*/site-packages，realpath 去重，
     避免 python3.1 前缀匹配到 python3.12 的重复）
  2. user-site: ~/.local/lib/python*/site-packages
  3. venv: home 内找 pyvenv.cfg（含隐藏目录，深度≤3；常规 walk 会剪掉 .venv 所以单独扫）
  4. system: /usr/local/lib/python3*/dist-packages（Debian 上 pip 装进系统的位置）

每个 *.dist-info 目录 = 一个 pip 包：版本取自目录名，Name/Summary 读 METADATA，
CLI 命令解析 entry_points.txt 的 [console_scripts]。
"""
import glob
import os
import re
import subprocess
import time

from .base import Backend, PackageRecord, user_home

_HEAVY = {".cache", "node_modules", "__pycache__", ".npm", ".bun", ".rustup"}
_CONDA_NAMES = ("miniconda3", "anaconda3", "miniforge3")


def _site_dirs(root):
    """一个 python 环境根下的 site-packages（realpath 去重）。"""
    out, seen = [], set()
    pats = (os.path.join(root, "lib", "python*", "site-packages"),
            os.path.join(root, "lib", "python*", "dist-packages"))
    for pat in pats:
        for d in glob.glob(pat):
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


def _conda_dists():
    """常见位置的 conda 发行版根（home、/opt、/usr/local）。"""
    out = []
    for base_dir in (user_home(), "/opt", "/usr/local"):
        for name in _CONDA_NAMES:
            p = os.path.join(base_dir, name)
            if os.path.isdir(os.path.join(p, "conda-meta")):  # conda-meta 才是真根
                out.append(p)
    return out


def _conda_info_envs(dist):
    """`conda info --envs` → [环境路径]。权威列表，含前缀式环境（-p 建的）。"""
    binary = os.path.join(dist, "bin", "conda")
    if not os.path.isfile(binary):
        return []
    try:
        p = subprocess.run([binary, "info", "--envs"],
                           capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
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


def _rel_or_abs(p):
    r = os.path.relpath(p, user_home())
    return p if r.startswith("..") else r


_ENV_CACHE = {"t": 0.0, "envs": None}


def find_python_envs():
    """→ [(label, [site_packages_dir])]，label 如 miniconda3/Pytorch、agent/.venv。
    覆盖：conda 命名环境 + 前缀式环境（-p）、venv、user-site、系统 pip。
    结果缓存 60s（一次采集里 collect 和 environment_summary 会各调一次）。"""
    now = time.time()
    if _ENV_CACHE["envs"] is not None and now - _ENV_CACHE["t"] < 60:
        return _ENV_CACHE["envs"]
    home = user_home()
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
    for dist in _conda_dists():
        dname = os.path.basename(dist)
        drp = os.path.realpath(dist)
        envs_dir = os.path.realpath(os.path.join(dist, "envs"))
        listed = _conda_info_envs(dist)
        if not listed:  # conda 二进制不可用时的兜底：base + envs/*
            listed = [drp]
            ed = os.path.join(dist, "envs")
            if os.path.isdir(ed):
                listed += [os.path.realpath(os.path.join(ed, e)) for e in sorted(os.listdir(ed))
                           if os.path.isdir(os.path.join(ed, e))]
        for ep in listed:
            if ep == drp:
                add_root(f"{dname}/base", ep)
            elif ep.startswith(envs_dir + os.sep):
                add_root(f"{dname}/{os.path.basename(ep)}", ep)
            else:  # 前缀式环境（conda create -p）
                add_root(_rel_or_abs(ep), ep)

    # 2) 兜底：扫 conda-meta 标记找未注册/二进制不可见的前缀环境
    for base_dir in (home, "/opt"):
        if not os.path.isdir(base_dir):
            continue
        for dirpath, dirnames, _fn in os.walk(base_dir):
            depth = dirpath[len(base_dir):].count(os.sep)
            if "conda-meta" in dirnames:
                add_root(_rel_or_abs(dirpath), dirpath)
                dirnames.remove("conda-meta")
            dirnames[:] = [d for d in dirnames if d not in _HEAVY]
            if depth >= 4:
                dirnames[:] = []

    # 3) venv（pyvenv.cfg，含隐藏目录）
    for base_dir in (home, "/opt"):
        if not os.path.isdir(base_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(base_dir):
            depth = dirpath[len(base_dir):].count(os.sep)
            if "pyvenv.cfg" in filenames:
                add_root(_rel_or_abs(dirpath), dirpath)
                dirnames[:] = []  # venv 内部不再深入
            else:
                dirnames[:] = [d for d in dirnames if d not in _HEAVY]
                if depth >= 3:
                    dirnames[:] = []

    # 4) user-site / 系统 pip（同样走 lib/python* 模式）
    add_root("user-site", os.path.join(home, ".local"))
    add_root("system", "/usr/local")

    envs, seen_sp, empties = [], set(), []
    for label, root in roots:
        keep = []
        for d in _site_dirs(root):
            rp = os.path.realpath(d)
            if rp not in seen_sp:  # 跨来源去重（同一 site-packages 只算一次）
                seen_sp.add(rp)
                keep.append(d)
        if keep:
            envs.append((label, keep))
        elif label not in ("user-site", "system"):  # 真环境但没 python = 空壳，记下
            empties.append(label)
    _ENV_CACHE.update(t=now, envs=envs, empties=empties)
    return envs


def environment_summary():
    """全部探测到的 Python 环境（含没有 python 的空壳）→ [{label, packages, empty?}]。"""
    out = []
    for label, sp_dirs in find_python_envs():
        n = sum(len(glob.glob(os.path.join(sp, "*.dist-info"))) for sp in sp_dirs)
        if n == 0 and label in ("user-site", "system"):  # 空伪根不占位
            continue
        out.append({"label": label, "packages": n})
    for label in _ENV_CACHE.get("empties") or []:
        out.append({"label": label, "packages": 0, "empty": True})
    return out


def _parse_dist_info(di):
    """解析一个 dist-info 目录 → (name, version, summary, [console_scripts])。"""
    base = os.path.basename(di)[:-len(".dist-info")]
    m = re.match(r"^(?P<name>.+)-(?P<ver>[A-Za-z0-9](?:[A-Za-z0-9.]*[A-Za-z0-9])?)$", base)
    name, ver = (m.group("name"), m.group("ver")) if m else (base, "?")
    summary = ""
    try:
        with open(os.path.join(di, "METADATA"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Summary: "):
                    summary = line[9:].strip()[:100]
                elif line.strip() == "":
                    break  # header 结束
    except OSError:
        pass
    exes = []
    try:
        with open(os.path.join(di, "entry_points.txt"), encoding="utf-8", errors="replace") as fh:
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


class PipBackend(Backend):
    pkg_type = "pip"

    @classmethod
    def available(cls):
        home = user_home()
        cands = [os.path.join(home, d) for d in _CONDA_NAMES]
        cands += glob.glob(os.path.join(home, ".local", "lib", "python*"))
        cands += glob.glob("/usr/local/lib/python3*")
        return any(os.path.isdir(c) for c in cands)

    def collect(self):
        records = []
        for label, sp_dirs in find_python_envs():
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
                        origin=f"pip({label})", install_path=sp,
                        executables=exes[:6], first_install=mtime,
                        extra={"env": label, "site_packages": sp, "summary": summary,
                               "class": "library",
                               "class_reason": "pip 包（卸载用 pip uninstall）"}))
        return records