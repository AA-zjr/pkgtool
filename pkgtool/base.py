"""pkgtool.base — 包格式后端的统一抽象。

新增一种包格式（rpm/snap/flatpak/appimage...）只需：
  1. 在本目录新建 <fmt>_backend.py，实现 Backend 子类（available + collect）
  2. 在 __init__.py 的 BACKENDS 列表里注册
  其他什么都不用改。
"""
import os
import pwd
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


def user_home():
    """真实用户主目录（用 passwd 数据库，不受 HOME 环境变量重写影响）。"""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:  # noqa: BLE001
        return os.path.expanduser("~")


_PRUNE_DIRS = {".cache", ".config", ".local", "node_modules", "__pycache__",
               ".venv", "venv", "snap", ".npm", ".bun", ".rustup"}


def scan_file_areas(suffixes, maxdepth=3):
    """扫描用户可写区域里的包文件（.deb/.AppImage…），不进入隐藏目录/缓存。
    覆盖：主目录(含中文“下载”)、/opt、/usr/local/bin、/tmp。"""
    home = user_home()
    roots = [home] + [p for p in ("/opt", "/usr/local/bin", "/tmp") if os.path.isdir(p)]
    seen = set()
    suf = tuple(s.lower() for s in suffixes)
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            dirnames[:] = [d for d in dirnames
                           if d not in _PRUNE_DIRS and not d.startswith(".")]
            if depth >= maxdepth:
                dirnames[:] = []
            for f in filenames:
                if f.lower().endswith(suf):
                    p = os.path.join(dirpath, f)
                    try:
                        rp = os.path.realpath(p)
                    except OSError:
                        continue
                    if rp not in seen:
                        seen.add(rp)
                        yield p


@dataclass
class PackageRecord:
    """一条已安装包的记录（跨格式统一结构）。"""
    pkg_type: str                    # deb / snap / flatpak / ...
    name: str                        # 包名（flatpak 用 app-id）
    version: str = "?"
    origin: str = "?"                # 来源：源标签/本地/flathub/snap store...
    install_path: str = ""           # 主要安装路径
    executables: list = field(default_factory=list)
    first_install: str = ""          # 首次安装时间（有日志才填）
    extra: dict = field(default_factory=dict)   # 格式特有字段，序列化进 CSV 的 extra 列

    def to_row(self):
        return [self.pkg_type, self.name, self.version, self.origin,
                self.install_path, ";".join(self.executables), self.first_install,
                " ".join(f"{k}={v}" for k, v in sorted(self.extra.items()))]


CSV_HEADER = ["pkg_type", "name", "version", "origin",
              "install_path", "executables", "first_install", "extra"]


class Backend(ABC):
    """一种包格式的后端。"""
    pkg_type: str = "?"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """该包管理体系在这台机器上是否存在（不抛异常）。"""

    @abstractmethod
    def collect(self) -> list:
        """返回 [PackageRecord, ...]。实现要求：
        - 优先读文件系统/缓存目录（离线可用、无需 root）
        - CLI 查询仅作增强，失败要能降级
        """
