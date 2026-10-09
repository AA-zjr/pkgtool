"""pkgtool.backends.appimage — AppImage 嗅探（纯被动，不执行目标文件）。

原理：AppImage v1/v2 = ELF 运行时 + 追加的 squashfs 负载。
  1. 扫描用户区域找 *.AppImage（主目录/下载、~/Applications、/opt…）
  2. 在文件里找 squashfs 超块（魔数 hsqs + block_size 是 4096~1MB 的 2 次幂
     + inode 数合理），避开 ELF 运行时内嵌 unsquashfs 代码里的字符串误报
  3. `unsquashfs -o <off> -l` 列出负载（只读目录树，不解压不执行）：
     取 .desktop 文件名作显示名、usr/bin/* 作可执行文件
  4. 版本号从文件名解析（CC-Switch-v3.20.4-Linux-x86_64 → 3.20.4）
位置语义：~/Applications、/opt、/usr/local/bin 下 = 已安置的便携应用；
下载等散落区域 = 未安装的包文件（extra["loose"]，与 deb 散落文件同构）。
"""
import os
import re
import shutil
import struct
import subprocess
import time

from ..base import Backend, OriginKind, PackageRecord, file_size_mb, scan_file_areas
from ..config import CFG

# 核心版本号 + 可选预发布后缀（beta/rc…），避免吞掉 -Linux-x86_64 这类架构尾巴
_VERSION_RE = re.compile(
    r"v?(\d+(?:\.\d+)+[A-Za-z0-9.+~]*"
    r"(?:-(?:beta|alpha|rc|dev|pre|nightly)[A-Za-z0-9.]*)?)")
_USR_BIN_RE = re.compile(r"(?:^|/)usr/bin/([^/]+)$")


def _probe(data):
    """在缓冲区里找合法的 squashfs 超块，返回绝对偏移；找不到 None。"""
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


def _squashfs_offset(path, limit):
    """找 squashfs 超块偏移；找不到返回 None。
    两段式读取：绝大多数 AppImage 的超块在文件头部，先读 64KB 快查，
    未命中才读全限——NTFS/FUSE 盘上 32MB 顺序读要秒级，常见路径
    把这段开销整个省掉。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(min(65536, limit))
            off = _probe(head)
            if off is not None or limit <= len(head):
                return off
            fh.seek(0)
            return _probe(fh.read(limit))
    except OSError:
        return None


def _payload_listing(path, offset, cfg):
    """unsquashfs -l → (显示名, [usr/bin 可执行])。失败返回 ("", [])。"""
    if not shutil.which("unsquashfs"):
        return "", []
    try:
        p = subprocess.run(["unsquashfs", "-q", "-o", str(offset), "-l", path],
                           capture_output=True, text=True, timeout=cfg.timeout_query)
    except (OSError, subprocess.SubprocessError):
        return "", []
    display, bins = "", []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line or "/" not in line:
            continue
        base = line.rsplit("/", 1)[-1]
        if not display and base.endswith(".desktop"):
            display = base[: -len(".desktop")]
        m = _USR_BIN_RE.search(line)
        if m and m.group(1) not in cfg.appimage_xdg_wrappers:
            bins.append(m.group(1))
    return display, bins[:cfg.appimage_max_exes]


def _version_from_name(stem):
    found = _VERSION_RE.findall(stem)
    return found[-1] if found else ""


class AppImageBackend(Backend):
    pkg_type = "appimage"

    @classmethod
    def available(cls, cfg=CFG):
        home = cfg.home
        cands = [os.path.join(home, d) for d in
                 cfg.appimage_app_dirs + cfg.appimage_download_dirs]
        cands += list(cfg.appimage_system_dirs)
        return any(os.path.isdir(d) for d in cands)

    def _settled_dirs(self):
        """已安置目录：这些位置下的 .AppImage 算便携应用，其余算散落文件。"""
        home = self.cfg.home
        return [os.path.join(home, d) for d in self.cfg.appimage_app_dirs] \
            + list(self.cfg.appimage_system_dirs)

    def _record(self, path, loose):
        cfg = self.cfg
        fn = os.path.basename(path)
        stem = fn.rsplit(".", 1)[0] if "." in fn else fn
        try:
            mtime = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
        except OSError:
            mtime = ""

        name, exes = stem, []
        off = _squashfs_offset(path, cfg.appimage_scan_limit)
        if off is not None:
            display, exes = _payload_listing(path, off, cfg)
            if display:
                name = display
        extra = {"found_at": path, "mtime": mtime}
        if loose:
            extra.update(loose=path, state="uninstalled",
                         found_in=os.path.basename(os.path.dirname(path)))
        return PackageRecord(
            pkg_type=self.pkg_type, name=name,
            version=_version_from_name(stem) or "?",
            # name 取自 .desktop 显示名，两个不同文件可能同名；用文件名做变体，
            # 否则 /opt 里已安置的和下载目录里散落的那份会互相顶掉
            variant=fn,
            origin_kind=OriginKind.FILE if loose else OriginKind.LOCAL,
            size_mb=file_size_mb(path),
            install_path=path, executables=exes, extra=extra)

    def collect(self):
        records, seen = [], set()

        def add(path, loose):
            try:
                rp = os.path.realpath(path)
            except OSError:
                return
            if rp in seen:
                return
            seen.add(rp)
            records.append(self._record(path, loose))

        for d in self._settled_dirs():
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".appimage"):
                    add(os.path.join(d, f), loose=False)
        for p in scan_file_areas((".appimage",), cfg=self.cfg):
            add(p, loose=True)
        return records
