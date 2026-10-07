"""pkgtool.apt.dpkg — /var/lib/dpkg 与 apt 标记状态的读取。

extended_states 是 auto/manual 标记的权威存储，兼容两种格式/位置：
  apt 3.x:  /var/lib/apt/extended_states   stanza 格式（Package: / Auto-Installed:）
  dpkg 旧版: /var/lib/dpkg/extended_states  行格式 "pkg:arch auto"
注意新版 dpkg(1.23+) 的 status 第三字段已不再区分 user/installed，不能再用它
判断 manual/auto，所以要与 apt-mark 交叉核对，不一致时标记 conflict。
"""
import os
import subprocess
from dataclasses import dataclass

from ..config import CFG


@dataclass(slots=True)
class StatusEntry:
    """/var/lib/dpkg/status 里一个包的全部所需字段。
    体积与依赖都在这里一次读出来，避免为算大小再去遍历文件树、
    为算依赖图再解析一遍 status。"""
    name: str
    version: str = ""
    status: str = ""
    installed_size_kb: int = 0
    depends: str = ""
    pre_depends: str = ""
    recommends: str = ""

    @property
    def size_mb(self):
        return round(self.installed_size_kb / 1024.0, 1)


_STATUS_FIELDS = {
    "Package": "name", "Version": "version", "Status": "status",
    "Installed-Size": "installed_size_kb", "Depends": "depends",
    "Pre-Depends": "pre_depends", "Recommends": "recommends",
}


def installed(cfg=CFG):
    """解析 dpkg status → {包名: StatusEntry}。
    多架构同名包（gcc:amd64 / gcc:i386）按名字归并，后者覆盖前者。"""
    pkgs = {}
    cur = {}

    def flush():
        name = cur.get("name")
        if name:
            size = cur.get("installed_size_kb", "")
            pkgs[name] = StatusEntry(
                name=name, version=cur.get("version", ""),
                status=cur.get("status", ""),
                installed_size_kb=int(size) if str(size).isdigit() else 0,
                depends=cur.get("depends", ""),
                pre_depends=cur.get("pre_depends", ""),
                recommends=cur.get("recommends", ""))
        cur.clear()

    try:
        fh = open(cfg.dpkg_status, errors="replace")
    except OSError:
        return pkgs
    with fh:
        for line in fh:
            if line == "\n":
                flush()
                continue
            if line[0] in " \t":          # 续行（Description 正文等）
                continue
            key, sep, value = line.partition(": ")
            if sep:
                field = _STATUS_FIELDS.get(key)
                if field and field not in cur:
                    cur[field] = value.strip()
    flush()
    return pkgs


def package_files(pkg, cfg=CFG):
    """从 <pkg>.list 读文件清单（比逐个 dpkg -L 快得多）→ (files, dirs)。"""
    path = os.path.join(cfg.dpkg_info_dir, pkg + ".list")
    files, dirs = [], []
    if not os.path.isfile(path):
        return files, dirs
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                p = line.rstrip("\n")
                if not p or p == "/":
                    continue
                (dirs if p.endswith("/") else files).append(p)
    except OSError:
        return files, dirs
    return files, dirs


def apt_marks(cfg=CFG):
    """→ (manual 集合, auto 集合)。apt-mark 不可用时返回两个空集。"""
    def run(sub):
        try:
            out = subprocess.run(["apt-mark", sub], capture_output=True,
                                 text=True, timeout=cfg.timeout_query).stdout
            return set(out.split())
        except (OSError, subprocess.SubprocessError):
            return set()
    return run("showmanual"), run("showauto")


def extended_states(cfg=CFG):
    """解析 extended_states → ({包名: 是否 auto}, 命中的文件路径)。
    只记录 auto 的包；未出现的包视为 manual。文件都不存在时返回 ({}, None)。"""
    for path in cfg.ext_states_paths:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, errors="replace") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            continue
        autos = {}
        if any(ln.startswith("Package: ") for ln in lines):      # stanza 格式
            name = None
            for ln in lines:
                if ln.startswith("Package: "):
                    name = ln.split(":", 1)[1].strip().split(":")[0]
                elif ln.startswith("Auto-Installed: ") and name is not None:
                    autos[name] = ln.split(":", 1)[1].strip() == "1"
                    name = None
        else:                                                    # 行格式
            for ln in lines:
                parts = ln.split()
                if len(parts) >= 2 and ":" in parts[0] and parts[1] in ("auto", "manual"):
                    autos[parts[0].split(":")[0]] = parts[1] == "auto"
        return autos, path
    return {}, None


def deb_file_info(path, *fields, cfg=CFG):
    """dpkg-deb -f 读散落 .deb 的元数据 → {字段: 值}；读不出包名返回 None。
    合并原先 scan_deb_files 与 _deb_file_info 两份同构实现。"""
    want = fields or ("Package", "Version")
    try:
        p = subprocess.run(["dpkg-deb", "-f", path, *want],
                           capture_output=True, text=True, timeout=cfg.timeout_query)
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for line in p.stdout.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            out[key.strip()] = value.strip()
    return out if out.get("Package") else None
