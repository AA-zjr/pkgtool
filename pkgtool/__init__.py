"""pkgtool — Debian 系本地软件包盘点与管理工具（零第三方依赖）。

分层（依赖自上而下，禁止反向引用）：
  config / compress / base / labels   内核：路径与阈值、压缩读取、数据模型、展示文案
  apt/                                deb 数据源与写操作：索引、日志、dpkg 状态、特权执行
  backends/                           各包格式采集器（deb/snap/flatpak/appimage）
  classify / remove                   卸载安全层：分类判定、残留扫描、dry-run 与执行
  inventory                           编排：discover → collect → classify，唯一数据入口
  report / cli                        输出与命令行

典型用法：
  from pkgtool import inventory
  inv = inventory.collect()
  for rec in inventory.select(inv, only_local=True):
      print(rec.name, rec.version)
"""
__version__ = "0.0.1"

from .base import (CSV_HEADER, Backend, Channel, OriginKind, PackageRecord,
                   PkgClass, is_safe_name)
from .backends import BACKENDS, available_backends, discover
from .config import CFG, Config

__all__ = ["CSV_HEADER", "Backend", "Channel", "OriginKind", "PackageRecord",
           "PkgClass", "is_safe_name", "BACKENDS", "available_backends",
           "discover", "CFG", "Config"]
