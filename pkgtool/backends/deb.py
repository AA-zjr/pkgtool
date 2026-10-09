"""pkgtool.backends.deb — deb/apt 已安装包 + 磁盘散落 .deb 文件的采集。

数据来源（按可信度排序）：
  1. /var/lib/dpkg/status       当前装了什么（名字/版本/状态）
  2. /var/lib/dpkg/info/*.list  每个包的文件清单（安装路径、可执行文件）
  3. /var/log/dpkg.log*         所有 dpkg 操作的时间线（含 dpkg -i，最接近事实）
  4. /var/log/apt/history.log*  仅 apt 系工具的操作记录（apt install ./x.deb 也在里面）
  5. extended_states            auto/manual 标记的权威存储
  6. /var/lib/apt/lists         当前已配置的源（apt 不持久记录"包从哪个源下载"，
                                只能用当前索引反推来源）

关键设计：安装通道(channel) 与 是否在源里(in_repo) 是两个正交维度，分开存。
原实现把两者挤进 extra["channel"] 一个字段，上层再用索引结论覆盖后端的日志结论，
154 个镜像自带包因此被误报成 "apt 源安装"，前端判预装的分支成了死代码。
"""
import os
import threading

from ..apt import deps, dpkg, lists, logs
from ..base import (Backend, Channel, OriginKind, PackageRecord, file_size_mb,
                    scan_file_areas)
from ..config import CFG


def resolve_channel(has_log, in_history, in_repo, cutoff, log_ts=None):
    """安装通道判定 —— 全项目唯一一处（原实现分散在 deb_backend.collect 和
    update_channels.deb_channels，两套规则还会互相覆盖）。

    优先级（从强证据到弱证据）：
      1. dpkg.log 无记录：apt history 有 → APT；否则在源里 → APT（日志已轮转，
         但 apt 能管）；都不满足 → UNKNOWN
      2. 首次安装落在系统出生时间窗内，且 apt history 无记录 → PREINSTALLED
         （history 优先于时间窗：镜像构建期的 apt 操作也会进 history）
      3. apt history 有记录：在源里 → APT；不在 → APT_LOCAL（源已移除/本地 .deb）
      4. 只有 dpkg.log 记录（= 有人跑过 dpkg -i）→ DPKG_LOCAL
         注意：即使这个包同时存在于源里，也判 DPKG_LOCAL —— 通道回答的是
         "怎么装进来的"，"能不能用 apt 升级"由 in_repo/candidate 单独回答。
    """
    if not has_log:
        if in_history:
            return Channel.APT
        return Channel.APT if in_repo else Channel.UNKNOWN
    if in_history:
        return Channel.APT if in_repo else Channel.APT_LOCAL
    if cutoff is not None:
        dt = logs.parse_ts(log_ts)
        if dt is not None and dt <= cutoff:
            return Channel.PREINSTALLED
    return Channel.DPKG_LOCAL


def resolve_origin(name, version, index):
    """来源判定 → (OriginKind, [源标签])。三级置信度：
      (包,版本) 精确命中某源 → REPO（高置信）
      包在源里但版本对不上   → REPO_STALE（源已更新，或源被移除后重新配置过）
      包不在任何源           → LOCAL（本地 .deb，高置信）
    """
    exact = index.repos(name, version)
    if exact:
        return OriginKind.REPO, exact
    any_ver = index.repos(name)
    if any_ver:
        return OriginKind.REPO_STALE, any_ver
    return OriginKind.LOCAL, []


def _top_dirs(files):
    """文件清单里的顶层目录（去重排序）。
    清单首行常是 "/."（dpkg 用来声明根目录所有权），lstrip 后会变成 "."，
    不排除就会在安装路径里显示成一个 meaningless 的点。"""
    out = set()
    for p in files:
        if not p.startswith("/"):
            continue
        head = p.lstrip("/").split("/")[0]
        if head and head != ".":
            out.add(head)
    return sorted(out)


class DebBackend(Backend):
    pkg_type = "deb"

    @classmethod
    def available(cls, cfg=CFG):
        return os.path.isfile(cfg.dpkg_status)

    def collect(self):
        cfg = self.cfg
        installed = dpkg.installed(cfg)
        # apt-mark 是两个 ~0.15s 的子进程，与其后的纯解析工作并发跑；
        # DepGraph 依赖 manual 集合，构建前 join。子进程等待释放 GIL，
        # 并行是实打实的墙钟收益。
        marks_box = {}

        def _apt_marks():
            marks_box["marks"] = dpkg.apt_marks(cfg)

        th = threading.Thread(target=_apt_marks, daemon=True)
        th.start()
        first_install, earliest = logs.dpkg_installs(cfg)
        history = logs.apt_history(cfg)
        ext_autos, ext_path = dpkg.extended_states(cfg)
        cutoff = logs.birth_cutoff(earliest, cfg)
        index = lists.load_index(cfg)
        th.join()
        manual, auto = marks_box["marks"]
        graph = deps.DepGraph(installed, manual)

        records = []
        for name in sorted(installed):
            entry = installed[name]
            version = entry.version
            files = dpkg.package_files(name, cfg)[0]
            exes = [p for p in files
                    if any(p.startswith(d + "/") for d in cfg.bin_dirs)
                    and os.access(p, os.X_OK)]
            top_dirs = _top_dirs(files)
            desktops = [f.rsplit("/", 1)[-1][: -len(".desktop")]
                        for f in files if f.endswith(".desktop")]

            log_ts, _log_ver = first_install.get(name, ("", ""))
            channel = resolve_channel(has_log=bool(log_ts),
                                      in_history=name in history,
                                      in_repo=name in index, cutoff=cutoff,
                                      log_ts=log_ts)
            mark = "manual" if name in manual else ("auto" if name in auto else "?")
            ext_mark = "absent" if ext_path is None else \
                ("auto" if ext_autos.get(name, False) else "manual")
            extra = {"apt_mark": mark, "ext_states": ext_mark, "top_dirs": top_dirs}
            # dpkg status 自带的 Essential/Priority 是空 apt 索引环境下
            # 卸载安全判定的最后依据（见 classify._deb 第一层）
            if entry.essential.strip().lower() == "yes":
                extra["essential"] = "yes"
            if entry.priority:
                extra["priority"] = entry.priority
            if mark != "?" and ext_mark != "absent" and mark != ext_mark:
                extra["mark_conflict"] = f"apt-mark={mark} extended_states={ext_mark}"
            if len(desktops) == 1:
                # 只有一个 .desktop 时才把它当作该包的应用标识。装了多个的包
                # （ubuntu-settings 顺带 gnome-initial-setup.desktop / info.desktop）
                # 拿第一个去匹配残留，会删到别的包的配置目录。
                extra["desktop_id"] = desktops[0]
            elif desktops:
                extra["desktop_files"] = desktops

            kind, repos = resolve_origin(name, version, index)
            candidate = index.candidate(name)
            own, dep_mb, _total = graph.total_mb(name)
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=name, version=version,
                origin_kind=kind, origin_repos=repos, channel=channel,
                in_repo=name in index,
                candidate=candidate if lists.is_upgrade(candidate, version) else "",
                size_mb=own, exclusive_deps=sorted(graph.exclusive(name)),
                deps_size_mb=dep_mb,
                install_path=";".join(top_dirs[:cfg.top_dirs_limit]),
                executables=exes, first_install=log_ts, extra=extra))

        records.extend(self._loose_files(installed, index))
        return records

    def _loose_files(self, installed, index):
        """磁盘上散落（未安装，或已安装但文件重复留着）的 .deb。"""
        out = []
        for path in scan_file_areas((".deb",), cfg=self.cfg):
            info = dpkg.deb_file_info(path, cfg=self.cfg)
            if not info:
                continue
            name, version = info["Package"], info.get("Version", "?")
            kind, repos = resolve_origin(name, version, index)
            out.append(PackageRecord(
                pkg_type=self.pkg_type, name=name, version=version,
                # 用文件名做变体：散落文件与同名已安装包是两条不同的事实
                #（一条说"装了什么"，一条说"磁盘上还留着多大的安装包"），
                # 合并成一条会把"这个 .deb 可以删了回收 220MB"的信息丢掉。
                variant=os.path.basename(path),
                origin_kind=OriginKind.FILE,
                in_repo=name in index,
                size_mb=file_size_mb(path), extra={
                    "loose": path,
                    "found_in": os.path.basename(os.path.dirname(path)),
                    "state": "duplicate" if name in installed else "uninstalled",
                    "repo_kind": kind.value, "repo_labels": repos}))
        return out
