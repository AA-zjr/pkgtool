"""pkgtool.base — 跨格式统一数据模型、后端契约、文件扫描。

这一层只定义"数据长什么样"，不含任何展示文案（见 labels.py）和采集逻辑
（见 backends/）。核心设计约束：

  · 结论一律用枚举 code 存，不存中文显示串。原实现把结论编码进
    origin="local(不在任何源)" / channel="preinstalled(镜像自带)" 这类字符串，
    三个调用方各自用 startswith 反解，字面量一改就静默出错。
  · 「安装通道」(channel) 与「当前是否在 apt 源里」(in_repo) 是两个正交维度，
    必须分开存。原实现挤在 extra["channel"] 一个字段里，导致后端算出的
    preinstalled 结论被上层用索引结论覆盖（154 个镜像自带包被误报成 apt 安装）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from .config import CFG

# 包名 / 版本 / flatpak app-id 的白名单。允许 epoch 的冒号、Debian 版本的 ~ +，
# 以及下划线——flatpak 的 app-id 规范允许 [A-Za-z0-9._-]，实际大量存在
# （app.zen_browser.zen），漏掉下划线会导致这些应用根本装不了。
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:+_\-~]{0,199}$")


def is_safe_name(value):
    """校验将被拼进 apt/snap/flatpak 命令行的标识符。
    首字符必须字母数字：拒绝前导 '-'（会被当成选项，属参数注入）、
    空白与 shell 元字符。所有写操作入口都必须先过这一关。"""
    return bool(value) and bool(_SAFE_NAME_RE.match(str(value)))


BYTES_PER_MB = 1048576

# 搜索时当成词分隔符的字符：空白 + app.zen_browser.zen 里的 . 和 _
_QUERY_SPLIT_RE = re.compile(r"[\s._\-/+:]+")


def query_tokens(q):
    """把查询串拆成小写 token：空白与 . _ - / + : 都算分隔符。
    整串子串匹配搜不到 "zen browser"→app.zen_browser.zen（一边是空格、
    一边是下划线），拆词后两边归一到同一形态才能对上。"""
    return [t for t in _QUERY_SPLIT_RE.split((q or "").lower()) if t]


def matches_tokens(haystack, tokens):
    """全部 token 都出现才算命中（AND 语义）。haystack 同样先做分隔符归一。"""
    if not tokens:
        return False
    h = " " + _QUERY_SPLIT_RE.sub(" ", (haystack or "").lower()) + " "
    return all(t in h for t in tokens)


def file_size_mb(path):
    """文件/目录占用（MB，一位小数）。目录递归累加，读不到的条目跳过。"""
    total = 0
    try:
        if os.path.isfile(path) or os.path.islink(path):
            total = os.path.getsize(path)
        else:
            for dirpath, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        continue
    except OSError:
        return 0.0
    return round(total / BYTES_PER_MB, 1)


def is_under_home(path, cfg=CFG):
    """路径是否在当前用户主目录内（决定删除要不要特权）。"""
    home = os.path.abspath(cfg.home) + os.sep
    return os.path.abspath(path).startswith(home)


def delete_paths(paths):
    """普通用户权限删除文件/目录 → (成功列表, 失败信息列表)。"""
    removed, failed = [], []
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed.append(p)
        except OSError as e:
            failed.append(f"{p}: {e}")
    return removed, failed


class Channel(str, Enum):
    """安装通道：这个包是怎么进到系统里的。"""
    APT = "apt"                    # 仓库安装，apt 可管理
    APT_LOCAL = "apt-local"        # apt install ./x.deb：历史有记录但当前无源可更新
    DPKG_LOCAL = "dpkg-local"      # dpkg -i 手动安装
    PREINSTALLED = "preinstalled"  # 镜像自带（出生时间窗内装入）
    UNKNOWN = "unknown"            # dpkg.log 与 apt history 都查无记录


class OriginKind(str, Enum):
    """来源：包从哪来。"""
    REPO = "repo"                  # (包, 版本) 精确命中某个已配置源
    REPO_STALE = "repo-stale"      # 包在源里但版本对不上：源已更新或源被移除
    LOCAL = "local"                # 不在任何源 = 本地 .deb
    FILE = "file"                  # 磁盘上散落的包文件（未安装 / 已安装的重复文件）


class PkgClass(str, Enum):
    """卸载风险分类：只有 APP 允许删（判定规则见 classify.py）。"""
    BASE = "base"
    LIBRARY = "library"
    SYSTEM = "system"
    APP = "app"
    FILE = "file"


def _code(v):
    return v.value if isinstance(v, Enum) else (v or "")


@dataclass
class PackageRecord:
    """一条已安装包的记录（跨格式统一结构）。"""
    pkg_type: str
    name: str
    version: str = "?"
    variant: str = ""            # 同名多实例的区分维度（flatpak 的 arch/branch）
    origin_kind: OriginKind = OriginKind.LOCAL
    origin_repos: list = field(default_factory=list)
    channel: Channel | None = None
    pkg_class: PkgClass | None = None
    class_reason: str = ""
    in_repo: bool = False
    candidate: str = ""
    size_mb: float = 0.0         # 包自身已安装体积
    exclusive_deps: list = field(default_factory=list)   # 只被它一个包依赖的包
    deps_size_mb: float = 0.0    # 上面那些独占依赖的体积合计
    install_path: str = ""
    executables: list = field(default_factory=list)
    first_install: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        """全局唯一键：(类型, 名称, 变体)。用于按名字定位记录与去重。"""
        return (self.pkg_type, self.name, self.variant)

    @property
    def upgradable(self):
        return bool(self.candidate)

    @property
    def is_loose_file(self):
        """磁盘上的包文件（不是一条已安装记录）。"""
        return bool(self.extra.get("loose"))

    @property
    def total_size_mb(self):
        """自身 + 独占依赖。独占依赖 = 只有这个包在用、没人共用的那些，
        删掉这个包它们也就没用了（定义见 apt/deps.py）。"""
        return round(self.size_mb + self.deps_size_mb, 1)

    def to_row(self):
        """→ CSV 行。全部用 code，extra 用 JSON（原先 k=v 空格拼接会被含空格的值破坏）。"""
        return [self.pkg_type, self.name, self.variant, self.version,
                _code(self.origin_kind), ";".join(self.origin_repos),
                _code(self.channel), int(self.in_repo), self.candidate,
                _code(self.pkg_class), self.class_reason,
                self.size_mb, len(self.exclusive_deps), self.deps_size_mb,
                self.total_size_mb, self.install_path,
                ";".join(self.executables), self.first_install,
                json.dumps(self.extra, ensure_ascii=False, sort_keys=True)]


CSV_HEADER = ["pkg_type", "name", "variant", "version", "origin_kind",
              "origin_repos", "channel", "in_repo", "candidate", "pkg_class",
              "class_reason", "size_mb", "exclusive_deps", "deps_size_mb",
              "total_size_mb", "install_path", "executables", "first_install",
              "extra"]


class Backend(ABC):
    """一种包格式的后端。新增格式 = 新建 backends/<fmt>.py + 在 backends/__init__ 注册。

    契约：只采集事实，不做分类判定（pkg_class 留给 inventory 层统一填，
    避免原实现里 deb 在后端分类、snap/flatpak 在 UI 分类的不对称）。
    """
    pkg_type: str = "?"

    def __init__(self, cfg=CFG):
        self.cfg = cfg

    @classmethod
    @abstractmethod
    def available(cls, cfg=CFG) -> bool:
        """该包管理体系在这台机器上是否存在（不得抛异常）。"""

    @abstractmethod
    def collect(self) -> list:
        """返回 [PackageRecord, ...]。实现要求：
        - 优先读文件系统/缓存目录（离线可用、无需 root）
        - CLI 查询仅作增强，失败要能降级
        """


def scan_file_areas(suffixes, maxdepth=None, cfg=CFG):
    """扫描用户可写区域里的包文件（.deb/.AppImage…），不进隐藏目录/缓存。
    覆盖：主目录（含中文"下载"）+ config.scan_system_roots。realpath 去重。"""
    maxdepth = cfg.scan_maxdepth if maxdepth is None else maxdepth
    suf = tuple(s.lower() for s in suffixes)
    seen = set()
    for root in cfg.scan_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            dirnames[:] = [d for d in dirnames
                           if d not in cfg.prune_dirs and not d.startswith(".")]
            if depth >= maxdepth:
                dirnames[:] = []
            for f in filenames:
                if not f.lower().endswith(suf):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    rp = os.path.realpath(p)
                except OSError:
                    continue
                if rp not in seen:
                    seen.add(rp)
                    yield p
