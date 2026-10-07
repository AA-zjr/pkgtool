#!/usr/bin/env python3
"""pkg_inventory.py — 统一软件盘点入口（deb + snap + flatpak，可扩展）。

用法:
  python3 pkg_inventory.py [输出.csv] [--all]
    --all  连 deb 的系统预装模块一起显示（默认隐藏）

输出 CSV 列: pkg_type, name, version, origin, install_path,
             executables, first_install, extra(格式特有字段 k=v;...)
新增包格式: 在 pkgtool/ 下加一个后端并注册，本文件无需改动。
"""
import csv
import os
import sys
from collections import Counter

from pkgtool import discover, CSV_HEADER


def main():
    raw = sys.argv[1:]
    show_all = "--all" in raw
    args = [a for a in raw if not a.startswith("-")]
    out_csv = args[0] if args else "pkg_inventory.csv"

    rows, hidden = [], 0
    per_type = Counter()
    for be in discover():
        recs = be.collect()
        per_type[be.pkg_type] = len(recs)
        print(f"[{be.pkg_type}] {len(recs)} 个", file=sys.stderr)
        for r in recs:
            if not show_all and str(r.extra.get("channel", "")).startswith("preinstalled"):
                hidden += 1
                continue
            rows.append(r.to_row())

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        w.writerows(rows)

    print(f"\n报告已写入: {os.path.abspath(out_csv)}  （--all 显示全部）")
    if hidden:
        print(f"已隐藏 deb 系统预装模块: {hidden} 个")
    print("\n=== 按包格式 ===")
    for k, v in per_type.most_common():
        print(f"  {k:<15}{v:>6}")

    # 来源概览（非 deb 类型 + deb 的本地/第三方源）
    oc = Counter()
    for r in rows:
        if r[0] == "deb":
            o = r[3]
            oc["deb: " + ("本地(不在任何源)" if o.startswith("local")
                           else "repo-版本已更新" if o.startswith("repo-")
                           else ("Ubuntu官方源" if "Ubuntu" in o else f"第三方源:{o.split(' | ')[0]}"))] += 1
        else:
            oc[f"{r[0]}: {r[3]}"] += 1
    print("\n=== 来源分布 ===")
    for k, v in oc.most_common(15):
        print(f"  {k:<60}{v:>5}")
    if len(oc) > 15:
        print(f"  ... 其余 {len(oc) - 15} 类见 CSV")


if __name__ == "__main__":
    main()