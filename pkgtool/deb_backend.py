"""pkgtool.deb_backend — deb/apt 包识别后端。

解析函数从 deb_inventory.py 原样迁移（行为不变）：
  dpkg status / dpkg.log* / apt history.log* / extended_states / apt lists
DebBackend.collect() 返回全部已安装包的 PackageRecord（含 preinstalled，
由上层决定是否隐藏）。
"""
#!/usr/bin/env python3
"""
deb_inventory.py — Debian 系已安装 deb 包盘点工具（第一步：识别与来源判定）

数据来源（按可信度排序）:
  1. /var/lib/dpkg/status          —— 当前装了什么（名字/版本/状态）
  2. /var/lib/dpkg/info/*.list     —— 每个包的文件清单（安装路径、可执行文件）
  3. /var/log/dpkg.log*            —— 所有 dpkg 操作的时间线（含 dpkg -i，最接近事实）
  4. /var/log/apt/history.log*     —— 仅 apt 系工具的操作记录
  5. extended_states               —— auto/manual 标记的权威存储，兼容两种格式/位置:
       · apt 3.x:  /var/lib/apt/extended_states   stanza 格式 (Package:/Auto-Installed:)
       · dpkg 旧版: /var/lib/dpkg/extended_states 行格式 "pkg:arch auto"
     注意: 新版 dpkg(1.23+) 的 /var/lib/dpkg/status 第三字段已不再区分 user/installed，
     不能再用它判断 manual/auto。与 apt-mark 输出交叉核对，不一致时标记 conflict。

用法:
  python3 deb_inventory.py [输出.csv] [--all]
    --all  连系统预装模块(preinstalled)一起显示（默认隐藏）

来源判定逻辑:
  - 包在 apt history 的 Install: 行里            -> apt（含 packagekit/GUI、apt install ./x.deb）
  - 不在 apt history，但 dpkg.log 有 install 事件
      · 时间接近系统"出生时间"(日志最早事件+24h) -> preinstalled（镜像自带/基础系统）
      · 明显晚于出生时间                          -> local-deb（用户 dpkg -i 手动安装）
  - 两个日志都查无记录                           -> unknown（日志轮转丢失/日志启用前安装）
"""
import csv
import glob
import gzip
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from collections import defaultdict

DPKG_INFO = "/var/lib/dpkg/info"
BIRTH_MARGIN_HOURS = 24  # 出生时间窗：日志最早事件后 24h 内装的视为镜像自带
BIN_DIRS = ("/bin", "/sbin", "/usr/bin", "/usr/sbin")


from .base import Backend, PackageRecord, scan_file_areas
from .safety import classify_deb

def open_any(f):
    """按扩展名打开可能压缩的文件。"""
    if f.endswith(".gz"):
        return gzip.open(f, "rt", errors="replace")
    if f.endswith((".xz", ".lzma")):
        import lzma
        return lzma.open(f, "rt", errors="replace")
    return open(f, "r", errors="replace")


def read_log_lines(patterns):
    """读取所有 dpkg/apt 日志（含 .gz 轮转文件）。"""
    for pat in patterns:
        for f in sorted(glob.glob(pat)):
            opener = gzip.open if f.endswith(".gz") else open
            try:
                with opener(f, "rt", errors="replace") as fh:
                    yield from fh
            except OSError:
                continue


def parse_dpkg_logs():
    """返回 {包名: (首次install时间, 版本)}，以及全局最早事件时间。"""
    first_install = {}
    earliest = None
    line_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) install (\S+) (\S+)")
    for line in read_log_lines(["/var/log/dpkg.log*"]):
        m = line_re.match(line)
        if not m:
            continue
        ts, pkg, ver = m.groups()
        pkg = pkg.split(":")[0]
        if earliest is None or ts < earliest:
            earliest = ts
        if pkg not in first_install:
            first_install[pkg] = (ts, ver)
    return first_install, earliest


def parse_apt_history():
    """返回 {包名: [(时间, 版本), ...]}，来自 apt history 的 Install/Upgrade 行。"""
    apt_pkgs = defaultdict(list)
    cur_ts = None
    for line in read_log_lines(["/var/log/apt/history.log*"]):
        if line.startswith("Start-Date:"):
            cur_ts = line.split(":", 1)[1].strip()
        elif line.startswith(("Install:", "Upgrade:")):
            # 条目形如 pkg:arch (version[, automatic])，版本括号内含逗号，不能用 split(",")
            for name, ver in re.findall(r"([\w.+-]+):[\w-]+ \(([^)]+)\)", line):
                apt_pkgs[name].append((cur_ts, ver))
    return apt_pkgs


def parse_dpkg_status():
    """解析 /var/lib/dpkg/status -> {包名: (版本, 状态标记)}"""
    pkgs = {}
    name, ver, status = None, None, None
    with open("/var/lib/dpkg/status", errors="replace") as fh:
        for line in fh:
            if line == "\n":
                if name:
                    pkgs[name] = (ver or "", status or "")
                name = ver = status = None
            elif line.startswith("Package: "):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version: "):
                ver = line.split(":", 1)[1].strip()
            elif line.startswith("Status: "):
                status = line.split(":", 1)[1].strip()
    if name:
        pkgs[name] = (ver or "", status or "")
    return pkgs


def apt_mark_sets():
    def run(cmd):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
            return set(out.split())
        except Exception:
            return set()
    return run(["apt-mark", "showmanual"]), run(["apt-mark", "showauto"])


# 可用环境变量 DEB_INV_EXT_STATES（冒号分隔）覆盖搜索路径，便于测试/chroot
_env_paths = os.environ.get("DEB_INV_EXT_STATES", "").split(":")
EXT_STATES_PATHS = tuple(p for p in _env_paths if p) or \
    ("/var/lib/apt/extended_states", "/var/lib/dpkg/extended_states")


def parse_extended_states():
    """解析 extended_states（auto 标记的权威存储）。返回 ({包名: True=auto}, 文件路径)。
    兼容两种格式：
      stanza 格式 (apt 3.x):   Package: foo / Architecture: amd64 / Auto-Installed: 1
      行格式 (dpkg 旧版):      "foo:amd64 auto"
    只记录 auto 的包；未出现的包视为 manual。文件都不存在时返回 ({}, None)。
    """
    for path in EXT_STATES_PATHS:
        if not os.path.isfile(path):
            continue
        with open(path, errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        autos = {}
        if any(ln.startswith("Package: ") for ln in lines):  # stanza 格式
            name = None
            for ln in lines:
                if ln.startswith("Package: "):
                    name = ln.split(":", 1)[1].strip().split(":")[0]
                elif ln.startswith("Auto-Installed: ") and name is not None:
                    autos[name] = ln.split(":", 1)[1].strip() == "1"
                    name = None
        else:                                                  # 行格式
            for ln in lines:
                parts = ln.split()
                if len(parts) >= 2 and ":" in parts[0] and parts[1] in ("auto", "manual"):
                    autos[parts[0].split(":")[0]] = parts[1] == "auto"
        return autos, path
    return {}, None


LISTS_DIR = "/var/lib/apt/lists"


def build_source_index():
    """构建安装源索引（识别来源的核心）。
    apt 不持久记录“包从哪个源下载”，所以用当前已配置源的索引反推：
      /var/lib/apt/lists/*_Packages*  每个源的全部 (包名,版本)
      /var/lib/apt/lists/*InRelease   每个源的 Label/Origin/Suite（用于命名）
    返回:
      avail:      {(包名, 版本): set(源标签)}   —— 精确匹配用
      pkg_labels: {包名: set(源标签)}           —— “包在源里但版本对不上”用
    """
    release_info = {}  # list前缀 -> "Label Suite"
    for f in glob.glob(os.path.join(LISTS_DIR, "*")):
        b = os.path.basename(f)
        if not b.endswith(("_InRelease", "_Release")) or b.endswith(".gpg"):
            continue
        prefix = b[: -len("_InRelease")] if b.endswith("_InRelease") else b[: -len("_Release")]
        label, suite = None, None
        try:
            with open_any(f) as fh:
                lines = fh.readlines()
            # InRelease 以 "-----BEGIN PGP SIGNED MESSAGE-----" 开头，字段在第一个空行之后；
            # 普通 Release 文件直接就是字段。到签名块为止。
            start = 0
            if lines and lines[0].startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
                for i, ln in enumerate(lines):
                    if i > 0 and ln.strip() == "":
                        start = i + 1
                        break
            for line in lines[start:]:
                if line.startswith("-----BEGIN PGP SIGNATURE-----"):
                    break
                if line.startswith("Label: "):
                    label = line.split(":", 1)[1].strip()
                elif line.startswith("Origin: ") and not label:
                    label = line.split(":", 1)[1].strip()
                elif line.startswith("Suite: "):
                    suite = line.split(":", 1)[1].strip()
        except OSError:
            continue
        release_info[prefix] = " ".join(x for x in (label or prefix, suite) if x)

    avail = defaultdict(set)
    pkg_labels = defaultdict(set)
    pkg_meta = {}  # 包名 -> {section, priority}（来自 apt 索引，供卸载分类用）
    for f in glob.glob(os.path.join(LISTS_DIR, "*_Packages*")):
        b = os.path.basename(f)
        if b.endswith((".gpg", ".gz.tmp")) or "InRelease" in b:
            continue
        name = b[:-3] if b.endswith(".gz") else b
        if not name.endswith("_Packages"):
            continue
        pfx = name[: -len("_Packages")]
        # 找最长的 Release 前缀匹配（Packages 文件名比 Release 多 component/arch 段）
        match = max((r for r in release_info if pfx == r or pfx.startswith(r + "_")),
                    key=len, default=None)
        tag = release_info[match] if match else pfx
        try:
            with open_any(f) as fh:
                cur = None
                for line in fh:
                    if line.startswith("Package: "):
                        cur = line.split(":", 1)[1].strip().split(":")[0]
                    elif line.startswith("Version: ") and cur:
                        ver = line.split(":", 1)[1].strip()
                        avail[(cur, ver)].add(tag)
                        pkg_labels[cur].add(tag)
                    elif line.startswith("Section: ") and cur:
                        pkg_meta.setdefault(cur, {})["section"] = line.split(":", 1)[1].strip()
                    elif line.startswith("Priority: ") and cur:
                        pkg_meta.setdefault(cur, {})["priority"] = line.split(":", 1)[1].strip()
        except OSError:
            continue
    return avail, pkg_labels, pkg_meta


def origin_of(pkg, ver, avail, pkg_labels):
    """三级置信度判定来源：
      1. (包,版本) 精确命中某源   -> 该源（高置信）
      2. 包在源里但版本对不上     -> 源已更新/源后被移除（中置信，标 repo-?）
      3. 包不在任何源             -> 本地 deb（高置信）
    """
    if (pkg, ver) in avail:
        return " | ".join(sorted(avail[(pkg, ver)]))
    if pkg in pkg_labels:
        return "repo-版本已更新(" + " | ".join(sorted(pkg_labels[pkg])) + ")"
    return "local(不在任何源)"


def scan_deb_files(dirs):
    """扫描保留的 .deb 文件作为本地安装的直接证据。返回 {包名: [(版本, 路径)]}。"""
    found = defaultdict(list)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.deb")):
            try:
                out = subprocess.run(["dpkg-deb", "-f", f, "Package", "Version"],
                                     capture_output=True, text=True, timeout=30).stdout
                # 输出可能是 "Package: x"/"Version: y" 或裸值两行，两种都兼容
                fields = {}
                for ln in out.splitlines():
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        fields[k.strip()] = v.strip()
                if len(fields) < 2:
                    vals = [v.strip() for v in out.splitlines() if v.strip()]
                    if len(vals) >= 2:
                        fields["Package"], fields["Version"] = vals[0], vals[1]
                if "Package" in fields and "Version" in fields:
                    found[fields["Package"]].append((fields["Version"], f))
            except Exception:
                continue
    return found


def package_files(pkg):
    """从 /var/lib/dpkg/info/<pkg>.list 读文件清单（比逐个 dpkg -L 快得多）。"""
    f = os.path.join(DPKG_INFO, pkg + ".list")
    files, dirs = [], []
    if os.path.isfile(f):
        with open(f, errors="replace") as fh:
            for line in fh:
                p = line.rstrip("\n")
                if not p or p == "/":
                    continue
                (dirs if p.endswith("/") else files).append(p)
    return files, dirs



def _deb_file_info(path):
    """dpkg-deb -f 读散落 .deb 的元数据 → {Package, Version}，失败 None。"""
    try:
        p = subprocess.run(["dpkg-deb", "-f", path, "Package", "Version"],
                           capture_output=True, text=True, timeout=30)
        d = {}
        for line in p.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                d[k.strip()] = v.strip()
        return d if d.get("Package") else None
    except Exception:  # noqa: BLE001
        return None


class DebBackend(Backend):
    pkg_type = "deb"

    @classmethod
    def available(cls):
        return os.path.isfile("/var/lib/dpkg/status")

    def collect(self):
        installed = parse_dpkg_status()
        first_install, birth_ts = parse_dpkg_logs()
        apt_pkgs = parse_apt_history()
        manual, auto = apt_mark_sets()
        ext_autos, ext_path = parse_extended_states()
        avail, pkg_labels, pkg_meta = build_source_index()
        birth_cutoff = None
        if birth_ts:
            try:
                birth_cutoff = datetime.strptime(birth_ts, "%Y-%m-%d %H:%M:%S") + timedelta(hours=BIRTH_MARGIN_HOURS)
            except ValueError:
                pass
        records = []
        for pkg in sorted(installed):
            ver, _status = installed[pkg]
            files, _dirs = package_files(pkg)
            exes = [p for p in files
                    if any(p.startswith(d + "/") for d in BIN_DIRS) and os.access(p, os.X_OK)]
            top = sorted({p.lstrip("/").split("/")[0] for p in files if p != "/"})[:4]

            in_apt = pkg in apt_pkgs
            fi_ts, _fi_ver = first_install.get(pkg, (None, None))
            if in_apt:
                channel = "apt"
            elif fi_ts is None:
                channel = "unknown(日志无记录)"
            else:
                try:
                    fi_dt = datetime.strptime(fi_ts, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    fi_dt = None
                if birth_cutoff is not None and fi_dt is not None and fi_dt <= birth_cutoff:
                    channel = "preinstalled(镜像自带)"
                else:
                    channel = "local-deb(用户dpkg -i)"

            mark = "manual" if pkg in manual else ("auto" if pkg in auto else "?")
            ext_mark = "absent" if ext_path is None else ("auto" if ext_autos.get(pkg, False) else "manual")
            conflict = ""
            if mark != "?" and ext_mark != "absent" and mark != ext_mark:
                conflict = f"aptmark={mark} ext={ext_mark}"

            extra = {"channel": channel, "apt_mark": mark, "ext_states": ext_mark}
            if conflict:
                extra["conflict"] = conflict
            # 卸载分类：base/library/system/app（只允许删 app，见 pkgtool/safety.py）
            meta = pkg_meta.get(pkg, {})
            desktops = [f.rsplit("/", 1)[-1][:-8] for f in files if f.endswith(".desktop")]
            has_desktop = bool(desktops)
            cls, reason = classify_deb(pkg, meta.get("section", ""), meta.get("priority", ""),
                                       exes, top, has_desktop, channel, mark)
            extra["class"] = cls
            extra["class_reason"] = reason
            if desktops:  # .desktop 的 ID，供卸载后残留匹配用
                extra["desktop_id"] = desktops[0]
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=pkg, version=ver,
                origin=origin_of(pkg, ver, avail, pkg_labels),
                install_path=";".join(top), executables=exes,
                first_install=fi_ts or "", extra=extra))
        # 磁盘上散落但未安装（或重复）的 .deb 文件
        for p in scan_file_areas((".deb",)):
            info = _deb_file_info(p)
            if not info:
                continue
            pname, pver = info["Package"], info.get("Version", "?")
            state = "已安装-重复文件" if pname in installed else "未安装"
            try:
                size_mb = round(os.path.getsize(p) / 1048576, 1)
            except OSError:
                size_mb = 0
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=pname, version=pver,
                origin=f"包文件({os.path.basename(os.path.dirname(p))})",
                install_path="", executables=[],
                extra={"loose": p, "state": state, "size_mb": size_mb,
                       "class": "file", "class_reason": "磁盘上的包文件"}))
        return records
