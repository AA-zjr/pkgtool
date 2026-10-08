"""pkgtool.report — 输出层：终端表格 / CSV / JSON / 详情。

只做"把数据变成文本"，不含任何判定逻辑（判定在 classify，文案在 labels）。
合并原先三份同构的汇总输出：Web UI 的来源分布柱状图、pkg_inventory.py 和
deb_inventory.py 各自的 Counter 聚合。
"""
import csv
import io
import json
import unicodedata
from collections import Counter
from enum import Enum

from . import labels
from .base import CSV_HEADER
from .classify import is_removable, visibility_level

# ---- 终端宽度对齐（中文占两列，直接 len() 会错位）----


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s, w, align="<"):
    s = str(s)
    gap = " " * max(0, w - width(s))
    return s + gap if align == "<" else gap + s


def truncate(s, w):
    s = str(s)
    if width(s) <= w:
        return s
    out = ""
    for c in s:
        if width(out) + width(c) > w - 1:
            break
        out += c
    return out + "…"


def render_table(rows, columns, indent="  "):
    """columns = [(表头, 取值函数, 最大宽度, 对齐)]。宽度按内容自适应，超上限截断。"""
    if not rows:
        return ""
    cells = []
    for head, get, maxw, align in columns:
        vals = [truncate(get(r), maxw) for r in rows]
        w = max(width(head), *(width(v) for v in vals))
        cells.append((head, vals, w, align))
    out = [indent + "  ".join(pad(h, w, a) for h, _v, w, a in cells)]
    out.append(indent + "  ".join("─" * w for _h, _v, w, _a in cells))
    for i in range(len(rows)):
        out.append(indent + "  ".join(pad(v[i], w, a) for _h, v, w, a in cells))
    return "\n".join(out)


# ---- list ----

# 列宽是刻意压过的：类型/名称/版本/大小/通道/类别/首装 合计约 114 列，
# 常见终端放得下。「来源」不在这张表里——它和「通道」高度重复，且 info 详情、
# summary 与 CSV 里都有。
_LIST_COLUMNS = [
    ("类型", lambda r: r.pkg_type, 10, "<"),
    ("名称", lambda r: r.name, 30, "<"),
    ("版本", lambda r: r.version + (" ↑" if r.upgradable else ""), 22, "<"),
    ("大小", lambda r: labels.size_pair_text(r), 13, ">"),
    ("通道", lambda r: labels.channel_short(r.channel), 9, "<"),
    ("类别", lambda r: labels.class_text(r.pkg_class), 8, "<"),
    ("首次安装", lambda r: (r.first_install or "—")[:10], 10, "<"),
]


def render_list(records, inv=None, show_all=False, level=0):
    parts = [render_table(records, _LIST_COLUMNS)]
    if not records:
        parts = ["  （无匹配记录）"]
    if inv is not None:
        hidden = sum(1 for r in inv.records
                     if visibility_level(r) > level)
        note = "" if show_all else f"，已隐藏 {hidden} 个依赖/系统组件（--all 显示全部）"
        up = sum(1 for r in records if r.upgradable)
        parts.append(f"\n  显示 {len(records)} / 共 {len(inv.records)} 条{note}"
                     + (f"；其中 {up} 个可升级（↑）" if up else "")
                     + f"；采集耗时 {inv.elapsed}s @ {inv.collected_at}")
        for be, err in inv.errors.items():
            parts.append(f"  !! 后端 {be} 采集失败：{err}")
    return "\n".join(parts)


# ---- 汇总 ----


def render_summary(inv, top=15):
    out = ["=== 按包格式 ==="]
    for k, v in Counter(r.pkg_type for r in inv.records).most_common():
        out.append(f"  {labels.PKG_TYPE_LABEL.get(k, k):<28}{v:>6}")

    out.append("\n=== 安装通道（deb 专属维度）===")
    ch = Counter(labels.channel_short(r.channel) for r in inv.records if r.channel)
    for k, v in ch.most_common():
        out.append(f"  {k:<28}{v:>6}")
    no_channel = sum(1 for r in inv.records if not r.channel)
    if no_channel:
        out.append(f"  （另有 {no_channel} 条非 deb 记录，不适用此维度）")

    out.append("\n=== 来源分布 ===")
    buckets = Counter(labels.origin_bucket(r) for r in inv.records)
    for k, v in buckets.most_common(top):
        out.append(f"  {truncate(k, 58):<60}{v:>5}")
    if len(buckets) > top:
        out.append(f"  ... 其余 {len(buckets) - top} 类见 CSV/JSON 输出")

    out.append("\n=== 卸载类别 ===")
    for k, v in Counter(labels.class_text(r.pkg_class) for r in inv.records).most_common():
        out.append(f"  {k:<28}{v:>6}")
    removable = sum(1 for r in inv.records if is_removable(r))
    out.append(f"  其中判定为“用户软件”、允许卸载的：{removable} 个")

    up = [r for r in inv.records if r.upgradable]
    if up:
        out.append(f"\n=== 可升级 {len(up)} 个 ===")
        for r in up[:top]:
            out.append(f"  {r.name:<34}{r.version} → {r.candidate}")
        if len(up) > top:
            out.append(f"  ... 其余 {len(up) - top} 个用 `pkgtool list --upgradable` 查看")

    if inv.errors:
        out.append("\n=== 采集失败的后端 ===")
        for be, err in inv.errors.items():
            out.append(f"  {be}: {err}")
    return "\n".join(out)


# ---- info ----


def render_detail(rec):
    """单包详情：结构化字段 + 全部 extra + 更新通道建议 + 能否卸载。"""
    kv = []

    def add(k, v):
        if v not in ("", None, [], {}):
            kv.append((k, str(v)))

    add("类型", labels.PKG_TYPE_LABEL.get(rec.pkg_type, rec.pkg_type))
    add("名称", rec.name)
    add("变体", rec.variant)
    add("版本", rec.version)
    add("安装通道", labels.channel_text(rec.channel))
    add("来源", labels.origin_text(rec))
    add("在当前 apt 源中", "是" if rec.in_repo else "否")
    if rec.upgradable:
        add("可升级到", rec.candidate)
    add("卸载类别", f"{labels.class_text(rec.pkg_class)}（{rec.class_reason}）")
    add("首次安装", rec.first_install)
    add("安装路径", rec.install_path)
    if rec.size_mb or rec.deps_size_mb:
        if rec.exclusive_deps:
            add("体积", f"自身 {labels.size_text(rec.size_mb)} + 独占依赖 "
                         f"{labels.size_text(rec.deps_size_mb)}"
                         f"（{len(rec.exclusive_deps)} 个）= "
                         f"{labels.size_text(rec.total_size_mb)}")
        else:
            add("体积", f"{labels.size_text(rec.size_mb)}（依赖都与其他包共用）")
    if rec.is_loose_file:
        add("文件状态", labels.loose_state(rec))
    add("可执行文件", f"{len(rec.executables)} 个")
    from . import launch            # 局部导入：输出层不静态依赖执行层
    argv = launch.plan(rec)
    add("启动命令", " ".join(argv) if argv else "（无启动入口，不是可运行的应用）")
    add("更新通道", labels.update_advice(rec))

    out = [f"{rec.name}  {rec.version}", "─" * max(20, width(rec.name) + width(rec.version) + 2)]
    w = max(width(k) for k, _ in kv)
    out += [f"  {pad(k, w)}  {v}" for k, v in kv]

    if rec.executables:
        out.append(f"\n  可执行文件（{len(rec.executables)}）:")
        out += [f"    {e}" for e in rec.executables]

    if rec.exclusive_deps:
        out.append(f"\n  独占依赖（{len(rec.exclusive_deps)} 个，只有这个包在用，"
                   f"合计 {labels.size_text(rec.deps_size_mb)}）:")
        out += [f"    {d}" for d in rec.exclusive_deps[:30]]
        if len(rec.exclusive_deps) > 30:
            out.append(f"    … 其余 {len(rec.exclusive_deps) - 30} 个")

    extra = {k: v for k, v in rec.extra.items()
             if k not in ("loose", "state", "desktop_id", "apt_mark",
                          "ext_states", "mark_conflict", "top_dirs",
                          "commands")}   # commands 已在「可执行文件」里列过
    if extra:
        out.append("\n  格式特有字段:")
        ew = max(width(k) for k in extra)
        out += [f"    {pad(k, ew)}  {v}" for k, v in sorted(extra.items())]

    marks = [(k, rec.extra[k]) for k in ("apt_mark", "ext_states", "mark_conflict")
             if rec.extra.get(k)]
    if marks:
        out.append("\n  apt 标记:")
        out += [f"    {k} = {v}" for k, v in marks]
    if is_removable(rec):
        out.append("\n  ✓ 允许卸载：pkgtool remove " + rec.name)
    else:
        out.append(f"\n  ✗ 不允许卸载：{labels.block_reason(rec.pkg_class)}")
    return "\n".join(out)


# ---- apt 仓库 ----


_SEARCH_COLUMNS = [
    ("包名", lambda r: r["name"], 34, "<"),
    ("版本", lambda r: r["version"], 24, "<"),
    ("大小", lambda r: f"{r['size_kb']}K", 8, ">"),
    ("源", lambda r: " ".join(x for x in (r["suite"], r["component"]) if x), 22, "<"),
    ("仓库更新", lambda r: r.get("date") or "—", 11, "<"),
    ("说明", lambda r: r.get("desc") or "", 46, "<"),
]


def render_search(results):
    if not results:
        return "  （未找到相关包）"
    return render_table(results, _SEARCH_COLUMNS)


def render_versions(info):
    """单包的全部候选版本。"""
    if not info:
        return "  （仓库中无此包）"
    cols = [("版本", lambda v: v["version"] + (" ←候选" if v["version"] == info["candidate"] else ""), 28, "<"),
            ("源", lambda v: v["repo"], 30, "<"),
            ("大小", lambda v: f"{v['size_kb']}K", 8, ">"),
            ("维护者", lambda v: v["maintainer"], 40, "<")]
    head = [f"{info['name']}：{len(info['versions'])} 个候选版本"]
    return "\n".join(head + [render_table(info["versions"], cols)])


# ---- 机器可读输出 ----


def render_csv(records):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for r in records:
        w.writerow(r.to_row())
    return buf.getvalue()


def _json_default(o):
    return o.value if isinstance(o, Enum) else str(o)


def render_json(inv, records=None):
    return json.dumps({
        "meta": {"collected_at": inv.collected_at, "elapsed": inv.elapsed,
                 "per_type": inv.per_type, "errors": inv.errors},
        "packages": [vars(r) for r in (records if records is not None else inv.records)],
    }, ensure_ascii=False, indent=2, default=_json_default)


def render_uninstall_list(records):
    """`pkgtool list --removable` 的精简视图：只列能删的。"""
    cols = [("类型", lambda r: r.pkg_type, 12, "<"),
            ("名称", lambda r: r.name, 34, "<"),
            ("版本", lambda r: r.version, 24, "<"),
            ("判定依据", lambda r: r.class_reason, 30, "<")]
    return render_table(records, cols)


_CATALOG_COLUMNS = [
    ("来源", lambda x: x.source, 7, "<"),
    ("", lambda x: "已装" if x.installed else "", 4, "<"),
    ("名称", lambda x: x.title, 22, "<"),
    ("安装标识", lambda x: x.name if x.name != x.title else "", 26, "<"),
    ("版本", lambda x: x.version or "—", 14, "<"),
    ("体积", lambda x: x.size_text or "—", 9, ">"),
    ("发布者/源", lambda x: x.publisher or "—", 16, "<"),
    ("说明", lambda x: x.summary or x.channel or "", 34, "<"),
]


def render_catalog(items):
    """跨源搜索结果（apt / snap / flatpak）。"""
    if not items:
        return "  （未找到相关条目）"
    from . import catalog
    out = [render_table(items, _CATALOG_COLUMNS)]
    el = " · ".join(f"{k} {v}s" for k, v in catalog.LAST_ELAPSED.items())
    out.append(f"\n  共 {len(items)} 条（{el}）")
    for e in catalog.LAST_ERRORS:
        out.append(f"  !! {e}")
    out.append("  安装：pkgtool install <安装标识> --source <来源>")
    return "\n".join(out)


def render_clean(targets):
    """`pkgtool clean --list`：可清理目标一览（每项两行：概要 + 路径与执行方式）。"""
    if not targets:
        return "  （没有可清理的目标）"
    from . import clean as _clean
    out = []
    for t in targets:
        head = pad(f"  {t.size_text:>9}", 14) \
            + truncate(labels.clean_label(t.kind, t.label), 46)
        if t.privileged:
            head += "  [需 sudo]"
        out.append(head)
        how = " ".join(t.argv) if t.argv else "删除文件"
        extra = " · ".join(x for x in (t.detail, how, t.note) if x)
        out.append(pad("", 14) + truncate(extra, 100))
    known = _clean.total_mb(targets)
    unknown = sum(1 for t in targets if not t.size_known)
    total = f"  合计可回收 {known / 1024:.2f} GB（{len(targets)} 项"
    total += f"，另有 {unknown} 项体积未知" if unknown else ""
    out.append(total + "）")
    return "\n".join(out)


def render_loose_list(records):
    """`pkgtool list --loose` 的视图：重点是路径和占多少空间。"""
    total = round(sum(r.size_mb for r in records), 1)
    cols = [("类型", lambda r: r.pkg_type, 10, "<"),
            ("名称", lambda r: r.name, 30, "<"),
            ("版本", lambda r: r.version, 22, "<"),
            ("大小", lambda r: labels.size_text(r.size_mb), 10, ">"),
            ("状态", lambda r: labels.loose_state(r), 16, "<"),
            ("路径", lambda r: r.extra.get("loose", ""), 60, "<")]
    rows = sorted(records, key=lambda r: -r.size_mb)
    table = render_table(rows, cols)
    dup = sum(1 for r in rows if r.extra.get("state") == "duplicate")
    note = f"\n  共 {len(rows)} 个包文件，占 {labels.size_text(total)}"
    if dup:
        note += f"；其中 {dup} 个已安装，文件只是留着占地方（可直接删）"
    return table + note
