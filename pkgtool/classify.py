"""pkgtool.classify — 卸载风险分类：全项目唯一判定入口。

只有 APP 类允许删除，其余一律拒绝（remove.py 会再校验一次）。

deb 的四层证据，判不准就保守归 SYSTEM：
  1. dpkg priority/section（来自 apt 索引）：required/important → BASE；
     section=libs/libdevel → LIBRARY；kernel/base → SYSTEM
  2. 命名规则：linux-*/xserver-*/firmware-*/nvidia-* → SYSTEM；
     lib*/-dev/-dbg → LIBRARY
  3. 文件布局：有 .desktop 或装进 /opt → APP（自带软件）；完全无可执行文件 → LIBRARY
  4. 安装意图：DPKG_LOCAL/APT_LOCAL/UNKNOWN 通道（= 用户自己拿本地 .deb 装的）
     → APP

原实现有两处问题在这里一并修掉：
  · /opt 规则是死代码：top_dirs 存的是单层目录名（"opt"），判定却写
    startswith("opt/")，永远不成立，文档里承诺的"装进 /opt 算 app"从未生效。
  · 文档写了 nvidia-* → system，代码里没有；补上（更保守，不会放开删除权限）。
分类只在 inventory 层调用一次，覆盖所有后端。原实现 deb 在后端分类、
snap/flatpak 在 Web UI 分类，两套入口导致规则漂移。
"""
import re

from .apt import lists as apt_lists
from .base import Channel, OriginKind, PkgClass

# 删掉就会让系统起不来的包，无论其他证据如何都不给删
CRITICAL = frozenset({
    "dpkg", "apt", "bash", "dash", "coreutils", "systemd", "init", "base-files",
    "base-passwd", "util-linux", "login", "passwd", "tar", "gzip", "sed", "grep",
    "findutils", "e2fsprogs", "kmod", "mount", "perl-base", "libc-bin", "debconf",
    "apt-utils", "policy-rc.d", "init-system-helpers",
})

_SYSTEM_PREFIXES = ("linux-", "xserver-", "firmware-", "nvidia-")
_LIBRARY_SUFFIXES = ("-dev", "-dbg", "-dbgsym")
_LIB_NAME_RE = re.compile(r"^lib[a-z0-9]+.*\d")
_SNAP_BASE_RE = re.compile(r"^(snapd|bare|core\d*|gnome-|gtk-common-themes|mesa-)")


def classify(rec, index=None):
    """→ (PkgClass, reason)。index 是 apt 仓库索引（提供 section/priority），可为 None。"""
    if rec.is_loose_file:
        return PkgClass.FILE, "磁盘上的包文件，不是已安装记录"
    t = rec.pkg_type
    if t == "deb":
        return _deb(rec, index)
    if t == "snap":
        return _snap(rec)
    if t.startswith("flatpak"):
        return _flatpak(rec)
    if t == "linyap":
        return _linyap(rec)
    if t == "appimage":
        return PkgClass.APP, "便携应用（用户自行安置）"
    return PkgClass.SYSTEM, f"未知包类型 {t}，保守拒绝"


def _deb(rec, index):
    name = rec.name
    meta = index.meta(name) if isinstance(index, apt_lists.RepoIndex) else {}
    section = meta.get("section", "")
    priority = meta.get("priority", "")
    top_dirs = rec.extra.get("top_dirs") or []
    has_desktop = bool(rec.extra.get("desktop_id") or rec.extra.get("desktop_files"))
    exes = rec.executables
    mark = rec.extra.get("apt_mark", "")

    if name in CRITICAL or priority in ("required", "important"):
        return PkgClass.BASE, f"基础包（priority={priority or 'critical-list'}）"
    if name.startswith(_SYSTEM_PREFIXES) or "firmware" in name \
            or section in ("kernel", "base"):
        return PkgClass.SYSTEM, "内核/固件/显卡驱动/X 服务"
    if has_desktop:
        return PkgClass.APP, "GUI 应用（带 .desktop）"
    if "opt" in top_dirs:
        return PkgClass.APP, "自带软件（装进 /opt）"
    if rec.channel in (Channel.DPKG_LOCAL, Channel.APT_LOCAL, Channel.UNKNOWN):
        # APT_LOCAL 也算：`apt install ./x.deb` 和 `dpkg -i x.deb` 一样，
        # 都是用户自己下载本地包装进来的，只是走了不同的安装器。
        # 但得有应用证据（桌面入口/可执行文件/装进 /opt）才给 APP：
        # cuda-repo、cuda-keyring 这类仓库/密钥/元包也是本地 .deb，标成
        # "软件"会误导用户以为可以启动甚至卸载（卸掉会连带弄坏 apt 源）。
        if has_desktop or exes or "opt" in top_dirs:
            return PkgClass.APP, "用户自行安装（本地 .deb）"
        return PkgClass.LIBRARY, "本地 .deb 但无应用入口（仓库/密钥/元包类载荷）"
    if section in ("libs", "libdevel"):
        return PkgClass.LIBRARY, f"section={section}"
    if _LIB_NAME_RE.match(name) or name.endswith(_LIBRARY_SUFFIXES):
        return PkgClass.LIBRARY, "命名规则（lib*/-dev/-dbg）"
    if not exes:
        return PkgClass.LIBRARY, "无可执行文件（库/数据/配置）"
    if mark == "manual":
        return PkgClass.APP, "用户主动安装（manual 标记）"
    return PkgClass.SYSTEM, "系统组件（保守默认）"


def _snap(rec):
    if _SNAP_BASE_RE.match(rec.name):
        return PkgClass.BASE, "snap 基础运行时"
    return PkgClass.APP, "snap 应用"


def _flatpak(rec):
    if rec.pkg_type.endswith("-runtime") or rec.extra.get("kind") == "runtime":
        return PkgClass.LIBRARY, "flatpak runtime（运行库）"
    return PkgClass.APP, "flatpak 应用"


def _linyap(rec):
    # base 层的 kind 也是 "runtime"，不看名字只看 kind
    if rec.extra.get("kind") in ("runtime", "base"):
        return PkgClass.LIBRARY, "玲珑运行时/基础环境（被应用引用，不单独卸载）"
    return PkgClass.APP, "玲珑应用"


def is_removable(rec):
    return rec.pkg_class is PkgClass.APP


def is_system_component(rec):
    """默认视图要隐藏的：镜像预装 + apt 自动标记的依赖/底层库。
    原前端用 channel.startswith('preinstalled') 判断，但 channel 已被上层覆盖，
    这条分支实际从未生效；现在 channel 只有一个来源，判定可靠。"""
    if rec.channel is Channel.PREINSTALLED:
        return True
    if rec.pkg_type != "deb":
        return False
    mark = rec.extra.get("apt_mark")
    return mark == "auto" or (mark == "?" and rec.extra.get("ext_states") == "auto")


def is_user_installed(rec):
    """"仅本地自装"过滤：用户自己动手装进来的东西。"""
    if rec.is_loose_file:
        return False
    t = rec.pkg_type
    if t == "deb":
        return (rec.origin_kind is OriginKind.LOCAL
                or rec.channel in (Channel.DPKG_LOCAL, Channel.APT_LOCAL))
    if t == "appimage":
        return True                     # 已安置的便携应用（散落文件上面已排除）
    if t == "linyap":
        return rec.extra.get("kind", "app") == "app"   # 运行时不算
    if t.endswith("-runtime"):
        return False
    return rec.origin_kind is OriginKind.LOCAL
