"""pkgtool — 跨包格式的软件盘点框架。

BACKENDS 即注册表：新增格式 = 新建 <fmt>_backend.py + 在这里加一行。
"""
from .base import Backend, PackageRecord, CSV_HEADER
from .deb_backend import DebBackend
from .snap_backend import SnapBackend
from .flatpak_backend import FlatpakBackend
from .appimage_backend import AppImageBackend
from .pip_backend import PipBackend

BACKENDS = [DebBackend, SnapBackend, FlatpakBackend, AppImageBackend, PipBackend]


def discover():
    """返回本机可用的后端实例列表。"""
    return [b() for b in BACKENDS if b.available()]