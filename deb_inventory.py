#!/usr/bin/env python3
"""deb_inventory.py — deb 包盘点（详细模式，保留原有 CLI）。

解析逻辑已迁移到 pkgtool/deb_backend.py（单一事实来源），本文件只负责
deb 专属的 CSV 列布局、--all/--scan-debs 过滤与汇总输出。
统一多格式报告请用 pkg_inventory.py。
"""
import csv
import os
import sys
from datetime import datetime, timedelta

from pkgtool.deb_backend import (
    BIRTH_MARGIN_HOURS, BIN_DIRS,
    apt_mark_sets, build_source_index, origin_of, parse_dpkg_logs,
    parse_dpkg_status, parse_apt_history, parse_extended_states,
    package_files, scan_deb_files,
)


def main():
    raw = sys.argv[1:]
    show_all = "--all" in raw
    scan_debs_flag = "--scan-debs" in raw
    # --scan-debs 后面的路径参数是扫描目录，不能混进输出文件名
    scan_dirs_arg = [a for a in raw[raw.index("--scan-debs") + 1:] if not a.startswith("-")] if scan_debs_flag else []
    args = [a for a in raw if not a.startswith("-") and a not in scan_dirs_arg]
    out_csv = args[0] if args else "deb_inventory.csv"

    print("[1/6] 读取 dpkg status ...", file=sys.stderr)
    installed = parse_dpkg_status()
    print(f"      共 {len(installed)} 个已安装包", file=sys.stderr)

    print("[2/6] 解析 dpkg.log（时间线）...", file=sys.stderr)
    first_install, birth_ts = parse_dpkg_logs()
    print(f"      日志覆盖起点: {birth_ts}", file=sys.stderr)

    print("[3/6] 解析 apt history.log ...", file=sys.stderr)
    apt_pkgs = parse_apt_history()
    print(f"      apt 记录过的包: {len(apt_pkgs)} 个", file=sys.stderr)

    print("[4/6] 读取 apt-mark 与 extended_states ...", file=sys.stderr)
    manual, auto = apt_mark_sets()
    ext_autos, ext_path = parse_extended_states()
    if ext_path is None:
        print("      未找到 extended_states（旧系统，仅用 apt-mark）", file=sys.stderr)
    else:
        print(f"      extended_states: {ext_path}（{len(ext_autos)} 个 auto）", file=sys.stderr)

    print("[5/6] 构建安装源索引（/var/lib/apt/lists）...", file=sys.stderr)
    avail, pkg_labels, _pkg_meta = build_source_index()
    print(f"      源中可用 (包,版本) {len(avail)} 条，覆盖 {len(pkg_labels)} 个包", file=sys.stderr)

    deb_evidence = {}
    if scan_debs_flag:
        # --scan-debs [目录...]：指定要扫描的目录，默认扫 home 及常见下载目录
        dirs = [d for d in scan_dirs_arg if os.path.isdir(d)] or \
               [os.path.expanduser("~"), os.path.expanduser("~/Downloads"), os.path.expanduser("~/下载")]
        print(f"[6/6] 扫描 .deb 文件: {dirs} ...", file=sys.stderr)
        deb_evidence = scan_deb_files(dirs)

    print("      逐包统计文件与可执行文件 ...", file=sys.stderr)
    birth_cutoff = None
    if birth_ts:
        try:
            birth_cutoff = datetime.strptime(birth_ts, "%Y-%m-%d %H:%M:%S") + timedelta(hours=BIRTH_MARGIN_HOURS)
        except ValueError:
            pass

    rows = []
    hidden = 0
    for pkg in sorted(installed):
        ver, status = installed[pkg]
        files, dirs = package_files(pkg)
        # 可执行文件：位于 bin/sbin 目录且当前确实有执行位
        exes = [p for p in files
                if any(p.startswith(d + "/") for d in BIN_DIRS) and os.access(p, os.X_OK)]
        # 主要安装路径：顶层目录（去重排序，最多列4个）
        top = sorted({p.lstrip("/").split("/")[0] for p in files if p not in ("/",)})[:4]

        in_apt = pkg in apt_pkgs
        fi_ts, fi_ver = first_install.get(pkg, (None, None))
        if in_apt:
            source = "apt"
        elif fi_ts is None:
            source = "unknown(日志无记录)"
        else:
            try:
                fi_dt = datetime.strptime(fi_ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                fi_dt = None
            if birth_cutoff is not None and fi_dt is not None and fi_dt <= birth_cutoff:
                source = "preinstalled(镜像自带)"  # 出生窗口内
            else:
                source = "local-deb(用户dpkg -i)"

        # 两个来源交叉核对 manual/auto
        mark = "manual" if pkg in manual else ("auto" if pkg in auto else "?")
        ext_mark = "absent" if ext_path is None else ("auto" if ext_autos.get(pkg, False) else "manual")
        conflict = ""
        if mark != "?" and ext_mark != "absent" and mark != ext_mark:
            conflict = f"aptmark={mark} ext={ext_mark}"

        origin = origin_of(pkg, ver, avail, pkg_labels)
        if deb_evidence.get(pkg):
            ev = ";".join(f"{v}:{p}" for v, p in deb_evidence[pkg])
            origin += " [deb文件:" + ev + "]"

        if source.startswith("preinstalled") and not show_all:
            hidden += 1
            continue
        rows.append([pkg, ver, source, fi_ts or "", mark, ext_mark, conflict,
                     origin, len(files), ";".join(top), ";".join(exes)])

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["package", "version", "source", "first_install",
                    "apt-mark", "ext-states", "mark-conflict", "origin",
                    "file_count", "top_dirs", "executables"])
        w.writerows(rows)

    # 汇总输出（含被隐藏的 preinstalled）
    from collections import Counter
    c = Counter(r[2] for r in rows)
    if hidden:
        c["preinstalled(镜像自带,已隐藏)"] = hidden
    conflicts = [r for r in rows if r[6]]
    print(f"\n报告已写入: {os.path.abspath(out_csv)}  （默认隐藏系统预装模块，--all 显示全部）")
    print(f"{'来源':<32}{'数量':>6}")
    for k, v in c.most_common():
        print(f"{k:<34}{v:>5}")
    if conflicts:
        print(f"\n!! manual/auto 两来源不一致的包（{len(conflicts)} 个）:")
        for r in conflicts[:20]:
            print(f"  {r[0]}: {r[6]}")

    # 来源汇总：把“安装通道 x 源”组合成最终结论
    oc = Counter()
    for r in rows:
        o = r[7]
        if o.startswith("local"):
            oc["本地deb(不在任何源)"] += 1
        elif o.startswith("repo-版本已更新"):
            oc["repo(版本已更新/源可能移除)"] += 1
        elif "Ubuntu" in o:
            oc["Ubuntu官方源(含镜像)"] += 1
        else:
            oc[f"第三方源: {o.split(' | ')[0]}"] += 1
    print("\n=== 安装来源分布 ===")
    for k, v in oc.most_common():
        print(f"  {k:<40}{v:>5}")
    print("\n=== 本地 deb（不在任何已配置源，用户自行下载安装的）===")
    for r in rows:
        if r[7].startswith("local"):
            print(f"  {r[0]}  {r[1]}  通道={r[2]}  {r[7]}")
    print("\n=== 判定为 local-deb（用户手动 dpkg -i）的包 ===")
    for r in rows:
        if r[2].startswith("local-deb"):
            print(f"  {r[0]}  {r[1]}  安装于 {r[3]}  可执行: {r[10] or '(无)'}")


if __name__ == "__main__":
    main()
