"""pkgtool.appimage_backend — AppImage 嗅探（纯被动，不执行目标文件）。

原理：AppImage v1/v2 = ELF 运行时 + 追加的 squashfs 负载。
  1. 扫描用户区域找 *.AppImage 文件（主目录/下载、~/Applications、/opt…）
  2. 在文件里找 squashfs 超块（魔数 hsqs + block_size 是 4096~1MB 的 2 次幂
     + inode 数合理），避开 ELF 运行时内嵌 unsquashfs 代码里的字符串误报
  3. `unsquashfs -o <off> -l` 列出负载（只读目录树，不解压不执行）：
     取 .desktop 文件名作显示名、usr/bin/* 作可执行文件
  4. 版本号从文件名解析（如 CC-Switch-v3.20.4-Linux-x86_64 → 3.20.4）
位置语义：~/Applications、/opt、/usr/local/bin 下 = 已安置的便携应用；
下载等散落区域 = 未安装的包文件（extra[loose]，与 deb 散落文件同构）。
"""
import os
import re
import shutil
import struct
import subprocess
import time

from .base import Backend, PackageRecord, scan_file_areas, user_home

APP_DIRS = ("Applications",)          # 主目录下
SYSTEM_APP_DIRS = ("/opt", "/usr/local/bin")


def _squashfs_offset(path, limit=32 * 1024 * 1024):
    """找 squashfs 超块偏移；找不到返回 None。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit)
    except OSError:
        return None
    i = 0
    while True:
        i = data.find(b"hsqs", i)
        if i < 0 or i + 16 > len(data):
            return None
        # 超块布局: magic(4) + inode_count(4) + creation_time(4) + block_size(4)
        inodes, _mtime, blk = struct.unpack("<III", data[i + 4:i + 16])
        if (blk and (blk & (blk - 1)) == 0 and 4096 <= blk <= 1 << 20
                and 0 < inodes < 10 ** 6):
            return i
        i += 1


def _payload_listing(path, offset):
    """unsquashfs -l → (显示名, [usr/bin可执行])。失败返回 ('', [])。"""
    if not shutil.which("unsquashfs"):
        return "", []
    try:
        p = subprocess.run(["unsquashfs", "-q", "-o", str(offset), "-l", path],
                           capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001
        return "", []
    display, bins = "", []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line or "/" not in line:
            continue
        base = line.rsplit("/", 1)[-1]
        if not display and base.endswith(".desktop"):
            display = base[:-8]
        m = re.search(r"(?:^|/)usr/bin/([^/]+)$", line)
        if m and m.group(1) not in ("xdg-open", "xdg-mime", "xdg-settings"):
            bins.append(m.group(1))
    return display, bins[:8]


def _version_from_name(stem):
    # 核心版本号 + 可选预发布后缀（beta/rc…），避免吞掉 -Linux-x86_64 这类架构尾巴
    m = re.findall(r"v?(\d+(?:\.\d+)+[A-Za-z0-9.+~]*"
                   r"(?:-(?:beta|alpha|rc|dev|pre|nightly)[A-Za-z0-9.]*)?)", stem)
    return m[-1] if m else ""


class AppImageBackend(Backend):
    pkg_type = "appimage"

    @classmethod
    def available(cls):
        home = user_home()
        cands = [os.path.join(home, d) for d in APP_DIRS] + list(SYSTEM_APP_DIRS)
        cands += [os.path.join(home, d) for d in ("下载", "Downloads", "桌面", "Desktop")]
        return any(os.path.isdir(d) for d in cands)

    def _record(self, path, loose):
        fn = os.path.basename(path)
        stem = fn.rsplit(".", 1)[0] if "." in fn else fn
        try:
            size_mb = round(os.path.getsize(path) / 1048576, 1)
            mtime = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
        except OSError:
            size_mb, mtime = 0, ""

        name, exes = stem, []
        off = _squashfs_offset(path)
        if off is not None:
            display, exes = _payload_listing(path, off)
            if display:
                name = display
        version = _version_from_name(stem) or "?"

        extra = {"found_at": path, "size_mb": size_mb, "mtime": mtime}
        if loose:
            extra["loose"] = path
            extra["state"] = "未安装(磁盘文件)"
            origin, cls, reason = "AppImage文件(未安装)", "file", "磁盘上的包文件"
        else:
            origin, cls, reason = "AppImage(便携)", "app", "便携应用(用户安置)"
        extra["class"] = cls
        extra["class_reason"] = reason
        return PackageRecord(pkg_type=self.pkg_type, name=name, version=version,
                             origin=origin, install_path=path, executables=exes,
                             extra=extra)

    def collect(self):
        records, seen = [], set()
        home = user_home()
        # 1) 已安置的便携应用：app 目录下的 .AppImage
        for d in [os.path.join(home, x) for x in APP_DIRS] + list(SYSTEM_APP_DIRS):
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.lower().endswith(".appimage"):
                    p = os.path.join(d, f)
                    try:
                        rp = os.path.realpath(p)
                    except OSError:
                        continue
                    if rp not in seen:
                        seen.add(rp)
                        records.append(self._record(p, loose=False))
        # 2) 散落文件（未安装）：用户区域
        for p in scan_file_areas((".appimage",)):
            try:
                rp = os.path.realpath(p)
            except OSError:
                continue
            if rp not in seen:
                seen.add(rp)
                records.append(self._record(p, loose=True))
        return records