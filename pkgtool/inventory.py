"""pkgtool.inventory — 采集编排：整个工具唯一的数据入口。

职责：跑后端 → 统一分类 → 去重 → 补更新信息。任何消费方（CLI、以后的别的前端）
都只从这里拿数据，不再自己加工。原实现把这些步骤放在 Web UI 的 collect() 里，
于是 CLI 和 UI 对同一份数据得出不同结论（UI 覆盖了后端算出的 channel，
CLI 没有，154 个镜像自带包在两边显示不一样）。

错误隔离：单个后端抛异常只记录到 errors，不影响其余后端出数据。
原实现整个 collect 包在一个 try 里，任一出错就整页空白。
"""
import time
from dataclasses import dataclass, field

from . import classify
from .backends import discover
from .backends.flatpak import check_updates as flatpak_check_updates
from .base import PackageRecord
from .config import CFG


@dataclass
class Inventory:
    records: list = field(default_factory=list)
    by_key: dict = field(default_factory=dict)
    per_type: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)      # 后端名 → 错误信息
    collected_at: str = ""
    elapsed: float = 0.0

    def __len__(self):
        return len(self.records)

    def get(self, pkg_type, name, variant=""):
        return self.by_key.get((pkg_type, name, variant))

    def find(self, name):
        """按名字找（类型/变体未知时用）→ [record]，可能多条（同名不同类型）。"""
        return [r for r in self.records if r.name == name]


def collect(cfg=CFG, check_updates=False, on_backend=None, only_types=None):
    """采集全部可用后端 → Inventory。
    check_updates=True 时才联网查 flathub 新版（约 2 秒），默认不查。
    only_types 给定则只跑这些类型的后端（如只要 deb 时跳过 snap/flatpak 的子进程探测）。"""
    t0 = time.time()
    records, per_type, errors = [], {}, {}
    for be in discover(cfg):
        if only_types and not any(be.pkg_type.startswith(t) for t in only_types):
            continue
        tb = time.time()
        try:
            recs = be.collect()
        except Exception as e:                     # noqa: BLE001 单后端失败不拖垮全局
            errors[be.pkg_type] = f"{type(e).__name__}: {e}"
            if on_backend:
                on_backend(be.pkg_type, 0, time.time() - tb, errors[be.pkg_type])
            continue
        per_type[be.pkg_type] = len(recs)
        records.extend(recs)
        if on_backend:
            on_backend(be.pkg_type, len(recs), time.time() - tb, "")

    index = _apt_index(cfg)
    for rec in records:
        if rec.pkg_class is None:
            rec.pkg_class, rec.class_reason = classify.classify(rec, index)

    by_key = _dedupe(records)
    if check_updates:
        _fill_flatpak_updates(by_key, cfg)

    return Inventory(records=sorted(by_key.values(), key=_sort_key),
                     by_key=by_key, per_type=per_type, errors=errors,
                     collected_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                     elapsed=round(time.time() - t0, 1))


def _apt_index(cfg):
    """apt 索引只在这里取一次；后端内部用的是同一份缓存。"""
    from .apt import lists
    try:
        return lists.load_index(cfg)
    except Exception:                              # noqa: BLE001 索引不可用时仍可分类
        return None


def _dedupe(records):
    """按 (类型,名称,变体) 去重：已安装记录优先于同名的散落包文件。"""
    by_key = {}
    for rec in records:
        prev = by_key.get(rec.key)
        if prev is None or (prev.is_loose_file and not rec.is_loose_file):
            by_key[rec.key] = rec
    return by_key


def _fill_flatpak_updates(by_key, cfg):
    try:
        updates = flatpak_check_updates(cfg)
    except Exception:                              # noqa: BLE001 联网失败只是没有更新信息
        return
    for rec in by_key.values():
        if rec.pkg_type.startswith("flatpak") and rec.name in updates:
            rec.candidate = updates[rec.name]      # 存远端显示名，仅作"有新版"标记
            rec.extra["update_name"] = updates[rec.name]


_TYPE_ORDER = {"deb": 0, "snap": 1, "flatpak": 2, "flatpak-runtime": 3,
               "linyap": 4, "appimage": 5}


def _sort_key(rec):
    return (_TYPE_ORDER.get(rec.pkg_type, 9), rec.name.lower(), rec.variant)


def select(inv, pkg_type=None, query=None, only_local=False, level=0,
           only_removable=False, only_upgradable=False, loose=None):
    """按条件筛选记录。全项目唯一的过滤实现。

    level 是披露层级（见 classify.visibility_level）：0=仅用户可用的软件，
    1=+库/数据/散落文件，2=全部。散落视图（loose=True）不受层级约束，
    那里本来就是专门看散落文件的。
    """
    q = (query or "").lower().strip()
    out = []
    for rec in inv.records:
        if pkg_type and pkg_type != "all":
            # flatpak 与 flatpak-runtime 归为一类，与旧 UI 的 chip 行为一致
            if not (rec.pkg_type == pkg_type
                    or (pkg_type == "flatpak" and rec.pkg_type.startswith("flatpak"))):
                continue
        if loose is not True and classify.visibility_level(rec) > level:
            continue
        if loose is not None and rec.is_loose_file != loose:
            continue
        if only_local and not classify.is_user_installed(rec):
            continue
        if only_removable and not classify.is_removable(rec):
            continue
        if only_upgradable and not rec.upgradable:
            continue
        if q and not _matches(rec, q):
            continue
        out.append(rec)
    return out


def _matches(rec, q):
    hay = " ".join((rec.pkg_type, rec.name, rec.version, rec.variant,
                    " ".join(rec.origin_repos), rec.channel.value if rec.channel else "",
                    rec.class_reason, rec.install_path,
                    " ".join(rec.executables), rec.first_install,
                    str(rec.extra.get("summary", "")))).lower()
    return q in hay


def hide_count(inv, level=0):
    """当前披露层级下隐藏了多少条（层级 0 时即"非软件"的总数）。"""
    return sum(1 for r in inv.records if classify.visibility_level(r) > level)


def upgradable(inv):
    return [r for r in inv.records if r.upgradable]


__all__ = ["Inventory", "collect", "select", "hide_count", "upgradable",
           "PackageRecord"]
