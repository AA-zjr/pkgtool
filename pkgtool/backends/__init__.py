"""pkgtool.backends — 各包格式采集器的注册表。

新增一种格式 = 新建 <fmt>.py 实现 Backend 子类 + 在 BACKENDS 里加一行，
其余模块（inventory / classify / report / cli / app）都不需要改。
"""
from ..base import Backend
from ..config import CFG
from .appimage import AppImageBackend
from .deb import DebBackend
from .flatpak import FlatpakBackend
from .snap import SnapBackend

BACKENDS = (DebBackend, SnapBackend, FlatpakBackend, AppImageBackend)


def available_backends(cfg=CFG):
    """本机存在对应包管理体系的后端类（available() 不得抛异常）。"""
    return [b for b in BACKENDS if b.available(cfg)]


def discover(cfg=CFG):
    """→ 本机可用后端的实例列表。"""
    return [b(cfg) for b in available_backends(cfg)]


__all__ = ["BACKENDS", "Backend", "available_backends", "discover",
           "DebBackend", "SnapBackend", "FlatpakBackend", "AppImageBackend"]
