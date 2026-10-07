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
from .classify import is_removable, is_system_component

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

_LIST_COLUMNS = [
    ("类型", lambda r: r.pkg_type, 16, "<"),
    ("名称", lambda r: r.name, 34, "<"),
    ("版本", lambda r: r.version + (" ↑" if r.upgradable else ""), 26, "<"),
    ("通道", lambda r: labels.channel_short(r.channel), 9, "<"),
    ("类别", lambda r: labels.class_text(r.pkg_class), 8, "<"),
    ("来源", lambda r: labels.origin_short(r), 30, "<"),
    ("首次安装", lambda r: (r.first_install or "—")[:10], 10, "<"),
]


def render_list(records, inv=None, show_all=False):
    parts = [render_table(records, _LIST_COLUMNS)]
    if not records:
        parts = ["  （无匹配记录）"]
    if inv is not None:
        hidden = sum(1 for r in inv.records if is_system_component(r))
        note = "" if show_all else f"，已隐藏 {hidden} 个系统预装/自动依赖（--all 显示）"
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

    if inv.py_envs:
        out.append("\n=== Python 环境（pip 嗅探覆盖）===")
        for e in inv.py_envs:
            note = " · 空壳（没装 python）" if e.get("empty") else ""
            out.append(f"  {e['label']:<44}{e['packages']:>5}{note}")

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
    if rec.is_loose_file:
        add("文件状态", labels.loose_state(rec))
        add("文件大小", f"{rec.extra.get('size_mb', '?')} MB")
    add("可执行文件", f"{len(rec.executables)} 个")
    add("更新通道", labels.update_advice(rec))

    out = [f"{rec.name}  {rec.version}", "─" * max(20, width(rec.name) + width(rec.version) + 2)]
    w = max(width(k) for k, _ in kv)
    out += [f"  {pad(k, w)}  {v}" for k, v in kv]

    if rec.executables:
        out.append(f"\n  可执行文件（{len(rec.executables)}）:")
        out += [f"    {e}" for e in rec.executables]

    extra = {k: v for k, v in rec.extra.items()
             if k not in ("loose", "state", "size_mb", "desktop_id", "apt_mark",
                          "ext_states", "mark_conflict", "top_dirs", "env")}
    if extra:
        out.append("\n  格式特有字段:")
        ew = max(width(k) for k in extra)
        out += [f"    {pad(k, ew)}  {v}" for k, v in sorted(extra.items())]

    for k in ("apt_mark", "ext_states", "mark_conflict"):
        if rec.extra.get(k):
            out.append(f"    {k} = {rec.extra[k]}")
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
                 "per_type": inv.per_type, "errors": inv.errors,
                 "python_environments": inv.py_envs},
        "packages": [vars(r) for r in (records if records is not None else inv.records)],
    }, ensure_ascii=False, indent=2, default=_json_default)


def render_uninstall_list(records):
    """`pkgtool list --removable` 的精简视图：只列能删的。"""
    cols = [("类型", lambda r: r.pkg_type, 12, "<"),
            ("名称", lambda r: r.name, 34, "<"),
            ("版本", lambda r: r.version, 24, "<"),
            ("判定依据", lambda r: r.class_reason, 30, "<")]
    return render_table(records, cols)
