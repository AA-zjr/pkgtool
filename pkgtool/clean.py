"""pkgtool.clean — 跨格式磁盘回收：扫描可清理目标 + 执行删除。

六类目标。能用官方清理命令的一律用官方命令——它们知道自己该删什么、不会
破坏自己的元数据；只有"磁盘上的散落包文件"和"用户缓存目录"才由
本工具直接删：

  loose           散落 .deb / .AppImage      直接删文件（主目录内不需要 root）
  apt-cache       /var/cache/apt/archives    apt-get clean
  snap-rev        非激活的旧修订             snap remove <name> --revision <rev>
  flatpak-unused  没有应用引用的运行时        flatpak uninstall --unused -y
  linyap-unused   玲珑未引用的 base/runtime   ll-cli prune（卸载应用后会残留）
  user-cache      ~/.cache 等用户缓存        直接删目录（XDG 规范：可再生的
                                             非必要数据，逐个顶层目录列出）
  conda-cache     <conda 根>/pkgs            conda clean -a -y

user-cache 的安全性依据：XDG 规范把 cache 目录定义为"可随时再生的非必要
数据"，应用的会话、凭据、配置都在 ~/.config / ~/.local，删缓存碰不到；
flatpak 沙盒应用的同位缓存（~/.var/app/*/cache）同理。小于
cache_min_mb 的条目不列，避免几百条 KB 级噪音淹没列表。

conda 的 pkgs 目录大量用硬链接（解包后的文件被链接进各环境），按文件体积
累加会高估可回收空间（实测 1.45 GB vs du 的 1.1 GB），所以只统计压缩包部分。
"""
import glob
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from .apt import actions
from .base import delete_paths, file_size_mb, is_safe_name, is_under_home
from .config import CFG
from .labels import loose_state, size_text

KINDS = ("loose", "apt-cache", "snap-rev", "flatpak-unused", "linyap-unused",
         "user-cache", "conda-cache")


@dataclass
class Target:
    """一个可清理目标。paths 与 argv 二选一：直接删文件，或执行官方清理命令。"""
    kind: str
    label: str
    detail: str
    size_mb: float = 0.0
    size_known: bool = True
    paths: list = field(default_factory=list)
    argv: list = field(default_factory=list)
    privileged: bool = False
    note: str = ""

    @property
    def size_text(self):
        return size_text(self.size_mb, self.size_known)


# ---------- 各类目标的扫描 ----------


def loose_target(rec, cfg=CFG):
    """一条散落文件记录 → 清理目标；文件已不在原位置时返回 None。
    磁盘回收视图与散落包文件视图共用，保证两处删除行为一致。"""
    path = rec.extra.get("loose")
    if not path or not os.path.exists(path):
        return None
    return Target(
        kind="loose", label=f"{rec.name} {rec.version}", detail=path,
        size_mb=rec.size_mb or file_size_mb(path), paths=[path],
        privileged=not is_under_home(path, cfg), note=loose_state(rec))


def _loose(inv, cfg):
    """磁盘上散落的 .deb / .AppImage。"""
    out = []
    for r in (inv.records if inv else []):
        if not r.is_loose_file:
            continue
        t = loose_target(r, cfg)
        if t:
            out.append(t)
    return out


def _apt_cache(inv, cfg):
    """apt 下载缓存。用 apt-get clean 而不是自己 rm：它还会清 partial/ 与索引临时文件。"""
    d = cfg.apt_archives_dir
    files = glob.glob(os.path.join(d, "*.deb")) + glob.glob(os.path.join(d, "*.ddeb"))
    if not files:
        return []
    total = round(sum(file_size_mb(f) for f in files), 1)
    return [Target(kind="apt-cache", label=f"{len(files)} 个 .deb/.ddeb",
                   detail=d,
                   size_mb=total, argv=["apt-get", "clean"], privileged=True,
                   note="只删已下载的包文件，需要时会重新下载")]


def _snap_revisions(inv, cfg):
    """snapd 保留的旧修订（非当前激活的那些）。"""
    from .backends.snap import SnapBackend
    if not SnapBackend.available(cfg):
        return []
    be = SnapBackend(cfg)
    out = []
    for name, revs in sorted(be.stored_revisions().items()):
        if not is_safe_name(name):
            continue
        active = be.active_revision(name, revs)
        for rev in sorted(revs, key=int):
            if rev == active:
                continue
            path = revs[rev]
            out.append(Target(
                kind="snap-rev", label=f"{name} rev{rev}", detail=path,
                size_mb=file_size_mb(path),
                argv=["snap", "remove", name, "--revision", rev],
                privileged=True, note=f"当前激活 rev{active}"))
    return out


def _flatpak_unused(inv, cfg):
    """没有任何应用引用的 flatpak 运行时。体积只有执行后才知道。"""
    from .backends.flatpak import FlatpakBackend
    if not FlatpakBackend.available(cfg) or not shutil.which("flatpak"):
        return []
    return [Target(kind="flatpak-unused", label="",
                   detail="flatpak uninstall --unused：卸载没有任何应用引用的运行时",
                   size_known=False, argv=["flatpak", "uninstall", "--unused", "-y"],
                   privileged=True, note="体积要执行后才知道")]


def _linyap_unused(inv, cfg):
    """玲珑卸载应用后残留的未引用 base/runtime（ll-cli prune 专清这个）。
    体积只有执行后才知道。"""
    if not shutil.which("ll-cli"):
        return []
    return [Target(kind="linyap-unused", label="",
                   detail="ll-cli prune：移除未被任何应用引用的基础环境/运行时",
                   size_known=False, argv=["ll-cli", "prune"],
                   privileged=True, note="体积要执行后才知道")]


def child_targets(path, cfg=CFG):
    """下钻：列出一个缓存目录下一层的子项（目录与文件），按体积降序。
    供磁盘回收视图逐层深入——tracker3/JetBrains 这类大缓存往往是其中
    某个子目录在膨胀，能定位到具体来源就不用整个目录陪葬。
    小于 cache_min_mb 的子项不列，与顶层扫描同一阈值。"""
    if not os.path.isdir(path):
        return []
    try:
        entries = os.listdir(path)
    except OSError:
        return []
    out = []
    for e in sorted(entries):
        p = os.path.join(path, e)
        size = file_size_mb(p)
        if size >= cfg.cache_min_mb:
            out.append(Target(kind="user-cache", label=e, detail=p,
                              size_mb=size, paths=[p],
                              note="可再生缓存，会话与配置不受影响"))
    out.sort(key=lambda t: -t.size_mb)
    return out


def _user_cache(inv, cfg):
    """~/.cache 与 flatpak 沙盒应用缓存（~/.var/app/*/cache）下的顶层目录。
    每个目录单独一条，用户可以只挑不要的删；小于 cfg.cache_min_mb 的不列。"""
    roots = [cfg.xdg_cache_home]
    var_app = cfg.flatpak_sandbox_home
    if os.path.isdir(var_app):
        for app in sorted(os.listdir(var_app)):
            p = os.path.join(var_app, app, "cache")
            if os.path.isdir(p):
                roots.append(p)
    out = []
    for root in roots:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for e in sorted(entries):
            path = os.path.join(root, e)
            size = file_size_mb(path)
            if size < cfg.cache_min_mb:
                continue
            out.append(Target(kind="user-cache", label=e, detail=path,
                              size_mb=size, paths=[path],
                              note="可再生缓存，会话与配置不受影响"))
    return out


def _conda_cache(inv, cfg):
    """conda 包缓存。用 conda clean -a：它知道哪些包还被环境引用着。
    conda 根目录由 config.conda_roots 定位——这里只是找缓存目录，
    不做任何环境探测（环境管理已整体移除）。"""
    out = []
    for dist in cfg.conda_roots:
        pkgs = os.path.join(dist, cfg.conda_pkgs_subdir)
        if not os.path.isdir(pkgs):
            continue
        # 只统计压缩包：解包后的目录被硬链接进各环境，删了也不释放空间
        tarballs = (glob.glob(os.path.join(pkgs, "*.conda"))
                    + glob.glob(os.path.join(pkgs, "*.tar.bz2")))
        size = round(sum(file_size_mb(t) for t in tarballs), 1)
        if not size:
            continue
        binary = os.path.join(dist, "bin", "conda")
        out.append(Target(
            kind="conda-cache",
            label=f"{os.path.basename(dist)} · {len(tarballs)} 个压缩包",
            detail=pkgs, size_mb=size,
            argv=[binary, "clean", "-a", "-y"] if os.path.isfile(binary) else [],
            paths=[] if os.path.isfile(binary) else tarballs,
            note="conda clean -a：清索引缓存、锁文件与未使用的包"))
    return out


COLLECTORS = (("loose", _loose), ("apt-cache", _apt_cache),
              ("snap-rev", _snap_revisions), ("flatpak-unused", _flatpak_unused),
              ("linyap-unused", _linyap_unused),
              ("user-cache", _user_cache), ("conda-cache", _conda_cache))


def collect_targets(cfg=CFG, kinds=None, inv=None, min_size_mb=0.0):
    """扫描全部（或指定类别的）可清理目标，按体积从大到小排。"""
    kinds = tuple(kinds) if kinds else KINDS
    if inv is None and "loose" in kinds:
        from . import inventory            # 延迟导入：只有需要散落文件时才做全量采集
        inv = inventory.collect(cfg)
    out = []
    for kind, fn in COLLECTORS:
        if kind not in kinds:
            continue
        try:
            out.extend(fn(inv, cfg))
        except Exception:                   # noqa: BLE001 一类扫不出来不该挡住其他类
            continue
    if min_size_mb > 0:
        # 显式给了体积下限就严格执行：体积未知的目标（flatpak 无用运行时）也一并
        # 排除。否则 "--min-size 500" 会连一个不知道多大的东西也删，不符合直觉。
        out = [t for t in out if t.size_known and t.size_mb >= min_size_mb]
    out.sort(key=lambda t: -t.size_mb)
    return out


def total_mb(targets):
    return round(sum(t.size_mb for t in targets if t.size_known), 1)


# ---------- 删除 ----------


def delete(target, trash=False, on_line=None, cfg=CFG):
    """执行一个目标 → actions.Result。
    trash=True 时对普通权限的文件类目标移到回收站（可还原）；
    特权目标和官方命令类目标不受影响，照常执行。"""
    if target.argv:
        if target.privileged:
            return actions.run_privileged(target.argv, timeout=cfg.timeout_remove,
                                          on_line=on_line, cfg=cfg)
        return actions.run_plain(target.argv, timeout=cfg.timeout_remove, cfg=cfg)
    if not target.paths:
        return actions.Result(ok=False, error="目标没有任何可删除的内容")
    if target.privileged:
        # root 拥有的文件进不了用户回收站，只能真删
        return actions.run_privileged(["rm", "-rf", "--", *target.paths],
                                      timeout=cfg.timeout_remove, on_line=on_line,
                                      cfg=cfg)
    if trash:
        return to_trash(target.paths, cfg)
    removed, failed = delete_paths(target.paths)
    return actions.Result(ok=not failed, file=",".join(removed),
                          error="; ".join(failed), command=["rm", "-rf", *target.paths])


def to_trash(paths, cfg=CFG):
    """移到 freedesktop 回收站（~/.local/share/Trash），可从文件管理器还原。
    跨文件系统时 shutil.move 会退化成复制+删除，大文件会慢。"""
    files_dir = os.path.join(cfg.trash_dir, "files")
    info_dir = os.path.join(cfg.trash_dir, "info")
    try:
        os.makedirs(files_dir, exist_ok=True)
        os.makedirs(info_dir, exist_ok=True)
    except OSError as e:
        return actions.Result(ok=False, error=f"无法创建回收站目录：{e}")
    moved, failed = [], []
    for p in paths:
        base = os.path.basename(p.rstrip(os.sep)) or "unnamed"
        dest, n = os.path.join(files_dir, base), 1
        while os.path.exists(dest):
            dest = os.path.join(files_dir, f"{base}.{n}")
            n += 1
        try:
            shutil.move(p, dest)
        except OSError as e:
            failed.append(f"{p}: {e}")
            continue
        try:
            with open(os.path.join(info_dir, os.path.basename(dest) + ".trashinfo"),
                      "w", encoding="utf-8") as fh:
                fh.write("[Trash Info]\n")
                fh.write(f"Path={quote(os.path.abspath(p))}\n")
                fh.write("DeletionDate="
                         + datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "\n")
        except OSError:
            pass                            # 文件已移走，元数据写失败不影响回收
        moved.append(p)
    return actions.Result(ok=not failed, file=",".join(moved),
                          error="; ".join(failed),
                          command=["gio", "trash", *paths])
