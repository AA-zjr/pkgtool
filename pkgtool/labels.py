"""pkgtool.labels — 枚举 code → 人类可读文案的唯一来源。

数据层（base/backends/classify）只产出 code，所有中文显示串集中在这里。
这样改文案不会动判定逻辑，反之亦然；原实现把文案和结论混在一个字符串里，
调用方靠 startswith 反解，两边改一处就静默错位。
"""
from .base import Channel, OriginKind, PkgClass

PKG_TYPE_LABEL = {
    "deb": "deb（apt/dpkg）",
    "snap": "snap（snap store）",
    "flatpak": "flatpak（应用）",
    "flatpak-runtime": "flatpak（运行库）",
    "appimage": "AppImage（便携）",
    "pip": "pip（Python 环境）",
}

CHANNEL_LABEL = {
    Channel.APT: "apt 源安装",
    Channel.APT_LOCAL: "apt install ./x.deb（当前无源可更新）",
    Channel.DPKG_LOCAL: "dpkg -i 手动安装（本地 .deb）",
    Channel.PREINSTALLED: "镜像自带（系统预装）",
    Channel.UNKNOWN: "未知（日志无记录，可能已轮转）",
}

ORIGIN_LABEL = {
    OriginKind.REPO: "已配置源",
    OriginKind.REPO_STALE: "源内版本已更新/源可能已移除",
    OriginKind.LOCAL: "本地（不在任何源）",
    OriginKind.FILE: "磁盘上的包文件",
}

CLASS_LABEL = {
    PkgClass.BASE: "基础包",
    PkgClass.LIBRARY: "库/依赖",
    PkgClass.SYSTEM: "系统组件",
    PkgClass.APP: "软件",
    PkgClass.FILE: "包文件",
}

BLOCK_REASONS = {
    PkgClass.BASE: "基础包，删除会破坏系统（dpkg/apt/bash 一类）",
    PkgClass.LIBRARY: "库/依赖，由软件自动管理，不应单独删除",
    PkgClass.SYSTEM: "系统组件，未通过“用户软件”判定，不给删",
    PkgClass.FILE: "散落包文件，不是已安装记录 —— 用 `pkgtool clean -k loose` 删除",
}

# 表格用的短标签（CHANNEL_LABEL/ORIGIN_LABEL 是给详情页用的完整文案）
CHANNEL_SHORT = {
    Channel.APT: "apt",
    Channel.APT_LOCAL: "apt本地",
    Channel.DPKG_LOCAL: "dpkg本地",
    Channel.PREINSTALLED: "预装",
    Channel.UNKNOWN: "未知",
}

ORIGIN_SHORT = {
    OriginKind.REPO: "源",
    OriginKind.REPO_STALE: "源(版本已变)",
    OriginKind.LOCAL: "本地",
    OriginKind.FILE: "包文件",
}

LOOSE_STATE_LABEL = {
    "duplicate": "已安装·重复文件",
    "uninstalled": "未安装",
}

CLEAN_KIND_LABEL = {
    "loose": "散落包文件",
    "apt-cache": "apt 下载缓存",
    "snap-rev": "snap 旧修订",
    "flatpak-unused": "flatpak 无用运行时",
    "pip-cache": "pip 缓存",
    "conda-cache": "conda 包缓存",
}

# 发行版官方源的 Label 关键字（取自 Release 文件的 Label/Origin，属外部数据而非本工具结论）
_DISTRO_LABELS = frozenset({"ubuntu", "debian"})


def channel_text(channel):
    if channel is None:
        return "—"
    return CHANNEL_LABEL.get(Channel(channel), str(channel))


def channel_short(channel):
    if channel is None:
        return "—"
    return CHANNEL_SHORT.get(Channel(channel), str(channel))


def origin_short(rec):
    """表格用的来源短标签：有源就显示源标签，否则显示来源分类。"""
    if rec.is_loose_file:
        return ORIGIN_SHORT[OriginKind.FILE]
    if rec.origin_repos:
        return rec.origin_repos[0]
    kind = OriginKind(rec.origin_kind) if rec.origin_kind else OriginKind.LOCAL
    return ORIGIN_SHORT.get(kind, "—")


def loose_state(rec):
    return LOOSE_STATE_LABEL.get(rec.extra.get("state", ""), rec.extra.get("state", ""))


def size_text(mb, known=True):
    """体积文案的唯一实现（clean / report / tui 都走这里）。"""
    if not known:
        return "未知"
    if mb <= 0:
        return "0"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB" if mb >= 1 else f"{mb * 1024:.0f} KB"


def size_pair_text(rec):
    """列表里的体积列：有独占依赖时显示「合计（自身+独占）」。"""
    if rec.exclusive_deps:
        return f"{size_text(rec.total_size_mb)} ({len(rec.exclusive_deps)})"
    return size_text(rec.size_mb)


def clean_label(kind, label=""):
    """磁盘回收条目的标题：类别文案 + 条目细节（细节为空时只显示类别）。"""
    head = CLEAN_KIND_LABEL.get(kind, kind)
    return f"{head} · {label}" if label else head


def class_text(pkg_class):
    if pkg_class is None:
        return "—"
    return CLASS_LABEL.get(PkgClass(pkg_class), str(pkg_class))


def origin_text(rec):
    """一行的来源描述：仓库标签优先，无仓库时用分类文案。"""
    kind = OriginKind(rec.origin_kind) if rec.origin_kind else OriginKind.LOCAL
    if rec.origin_repos:
        repos = " | ".join(rec.origin_repos)
        return f"{repos}（{ORIGIN_LABEL[kind]}）" if kind is OriginKind.REPO_STALE else repos
    return ORIGIN_LABEL[kind]


def is_distro_official(rec):
    """来源是否为发行版官方源（Ubuntu/Debian，含其镜像）。
    源标签形如 "Ubuntu noble/main"，首段就是 Release 文件的 Label。"""
    return any(lbl.split(" ", 1)[0].lower() in _DISTRO_LABELS
               for lbl in rec.origin_repos)


def origin_bucket(rec):
    """来源分布聚合用的分组标签（合并原先散在 UI 和两个 CLI 里的三份同构逻辑）。"""
    t = rec.pkg_type
    if rec.is_loose_file:
        return f"{t}: 磁盘包文件（散落）"
    kind = OriginKind(rec.origin_kind) if rec.origin_kind else OriginKind.LOCAL
    if t != "deb":
        return f"{t}: {rec.origin_repos[0] if rec.origin_repos else ORIGIN_LABEL[kind]}"
    if kind is OriginKind.LOCAL:
        return "deb: 本地（不在任何源）"
    if kind is OriginKind.REPO_STALE:
        return "deb: 源内版本已更新/源可能已移除"
    if is_distro_official(rec):
        return "deb: 发行版官方源（含镜像）"
    first = rec.origin_repos[0] if rec.origin_repos else "?"
    return f"deb: 第三方源 {first}"


def update_advice(rec):
    """更新通道建议：完全由结构化字段推导，不写死任何应用名。"""
    t, n = rec.pkg_type, rec.name
    if rec.is_loose_file or t == "appimage":
        return ("便携文件/散落包文件，无系统级更新通道 —— "
                "到官方渠道下载新版覆盖，或删掉重新安装")
    if t == "snap":
        return f"snap store 自动刷新；手动强制：sudo snap refresh {n}"
    if t.startswith("flatpak"):
        scope = "--user " if rec.extra.get("installation") == "user" else ""
        return f"flathub 推送；手动：flatpak update {scope}{n}"
    if t == "pip":
        env = rec.extra.get("env", "")
        return (f"Python 环境内更新：pip install -U {n}"
                + (f"（env: {env}）" if env else ""))
    if t == "deb":
        if rec.upgradable:
            return (f"apt 源管理，可升级 {rec.version} → {rec.candidate}："
                    f"sudo apt-get install -y {n}={rec.candidate}")
        if rec.channel is Channel.PREINSTALLED:
            return f"镜像自带，随系统整体升级：sudo apt-get upgrade"
        if rec.channel is Channel.APT_LOCAL:
            return ("apt install ./x.deb 装的，当前无源可更新 —— "
                    "由软件自带的检查更新推送，或重新下载 .deb 覆盖安装")
        if rec.channel is Channel.DPKG_LOCAL:
            return ("dpkg -i 手动装的本地 .deb —— "
                    "由软件自带的检查更新推送，或下载新 .deb 覆盖安装")
        if rec.in_repo:
            return f"apt 源管理，仓库内已是最新：sudo apt-get install -y {n}"
        return "不在任何已配置源中，无法用 apt 升级 —— 走软件自带推送"
    return "—"


def block_reason(pkg_class):
    if pkg_class is None:
        return "未知包，拒绝操作"
    return BLOCK_REASONS.get(PkgClass(pkg_class), "未知包，拒绝操作")
