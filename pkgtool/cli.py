"""pkgtool.cli — 命令行入口（`pkgtool` / `python3 -m pkgtool`）。

约定：进度与诊断写 stderr，数据写 stdout，所以 `pkgtool list --names-only | xargs …`
这类管道不会被进度输出污染。

特权命令（remove / upgrade）不在本进程里处理密码：直接用 sudo，让它在终端上
自己提示。原 Web UI 需要把密码从浏览器 POST 过来，CLI 下这条路整个删掉。
"""
import argparse
import sys

from . import inventory, labels, report
from .apt import actions, lists
from .backends import pip as pip_backend
from .config import CFG
from .remove import execute, preview

_TYPES = ("deb", "snap", "flatpak", "appimage", "pip")


def _err(msg):
    print(msg, file=sys.stderr)


def _confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _collect(args):
    """带进度输出的采集。"""
    quiet = getattr(args, "quiet", False)

    def on_backend(pkg_type, n, elapsed, error):
        if error:
            _err(f"[{pkg_type}] 采集失败: {error}")
        elif not quiet:
            _err(f"[{pkg_type}] {n} 个 ({elapsed:.1f}s)")

    only = None
    t = getattr(args, "type", None)
    if t and t != "all":
        only = [t]
    return inventory.collect(CFG, check_updates=getattr(args, "check_updates", False),
                             on_backend=on_backend, only_types=only)


def _write(text, path=None):
    if not path:
        print(text)
        return 0
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    _err(f"已写入 {path}")
    return 0


def _resolve(inv, name, pkg_type=None):
    """按名字定位唯一记录，供 remove/upgrade 这类写操作用。
    优先已安装记录：磁盘上同名的散落 .deb 既不可卸载也不可升级，把它算进
    歧义会让 `pkgtool remove wechat` 变成必须先加 --type 才能用。"""
    matches = [r for r in inv.records
               if r.name == name and (not pkg_type or pkg_type == "all"
                                      or r.pkg_type.startswith(pkg_type))]
    installed = [r for r in matches if not r.is_loose_file]
    if installed:
        matches = installed
    if not matches:
        _err(f"未找到 {name}（用 `pkgtool list --all -q {name}` 确认，"
             f"系统预装/自动依赖默认不显示）")
        return None
    if len(matches) > 1:
        _err(f"{name} 匹配到多条，请用 --type 指定：")
        for r in matches:
            _err(f"  {r.pkg_type:<16}{r.name} {r.version} {r.variant}")
        return None
    return matches[0]


# ---------- 子命令实现 ----------


def cmd_list(args):
    inv = _collect(args)
    recs = inventory.select(inv, pkg_type=args.type, query=args.query,
                            only_local=args.local, show_system=args.all,
                            env=args.env, only_removable=args.removable,
                            only_upgradable=args.upgradable,
                            loose=True if args.loose else None)
    if args.names_only:
        return _write("\n".join(r.name for r in recs), args.output)
    if args.format == "csv":
        return _write(report.render_csv(recs), args.output)
    if args.format == "json":
        return _write(report.render_json(inv, recs), args.output)
    if args.removable:
        text = report.render_uninstall_list(recs)
    elif args.loose:
        text = report.render_loose_list(recs)
    else:
        text = report.render_list(recs, inv, show_all=args.all)
    return _write(text, args.output)


def cmd_summary(args):
    inv = _collect(args)
    return _write(report.render_summary(inv, top=args.top), args.output)


def cmd_info(args):
    inv = _collect(args)
    matches = [r for r in inv.records if r.name == args.name
               and (args.type in (None, "all") or r.pkg_type.startswith(args.type))]
    if not matches:
        matches = [r for r in inv.records if args.name.lower() in r.name.lower()]
        if len(matches) > 8:
            _err(f"{args.name} 不是精确包名，相似的名字有 {len(matches)} 个，前 8 个：")
            for r in matches[:8]:
                _err(f"  {r.pkg_type:<16}{r.name}")
            return 1
        if not matches:
            _err(f"未找到 {args.name}")
            return 1
    return _write("\n\n".join(report.render_detail(r) for r in matches))


def cmd_envs(args):
    envs = pip_backend.environment_summary(CFG)
    if not envs:
        return _write("  （未探测到 Python 环境）")
    cols = [("环境", lambda e: e["label"], 52, "<"),
            ("包数", lambda e: str(e["packages"]), 8, ">"),
            ("备注", lambda e: "空壳（没装 python）" if e.get("empty") else "", 20, "<")]
    return _write(report.render_table(envs, cols))


def cmd_search(args):
    idx = lists.load_index(CFG)
    if not len(idx):
        _err("apt 索引为空：先跑 sudo apt-get update")
        return 1
    return _write(report.render_search(idx.search(args.query, args.limit)))


def cmd_show(args):
    idx = lists.load_index(CFG)
    info = idx.info(args.name)
    if not info:
        _err(f"仓库中没有 {args.name}")
        return 1
    return _write(report.render_versions(info))


def cmd_download(args):
    res = actions.download(args.name, args.version or "", dest_dir=args.dest, cfg=CFG)
    if res.ok:
        print(f"✓ 已下载 {res.file}")
        return 0
    _err(f"下载失败：{res.error}")
    return 1


def cmd_upgrade(args):
    if args.all:
        _err("升级全部（apt-get upgrade）…")
        res = actions.run_privileged(["apt-get", "upgrade", "-y"],
                                     timeout=CFG.timeout_upgrade,
                                     on_line=print, cfg=CFG)
        return 0 if res.ok else _fail(res)
    if not args.names:
        inv = _collect(args)
        up = inventory.upgradable(inv)
        if not up:
            print("没有可升级的包（索引可能过期，先跑 sudo apt-get update）")
            return 0
        print(report.render_list(up, inv))
        print("\n指定包名升级，或用 --all 升级全部。")
        return 0
    inv = _collect(args)
    failed = 0
    for name in args.names:
        rec = _resolve(inv, name, args.type)
        if rec is None:
            failed += 1
            continue
        _err(f"→ 升级 {rec.pkg_type} {rec.name} {rec.version}")
        res = _upgrade_one(rec)
        if res is not None and not res.ok:
            failed += 1
            _err(f"  失败：{res.error}")
    return 1 if failed else 0


def _upgrade_one(rec):
    """按包格式分派升级方式（原 Web UI 里是三个互不复用的接口）。
    返回 None 表示该格式没有系统级更新通道，只打印建议、不算失败。"""
    t, n = rec.pkg_type, rec.name
    if t == "deb":
        return actions.upgrade(n, rec.candidate or "", on_line=print, cfg=CFG)
    if t == "snap":
        return actions.run_privileged(["snap", "refresh", n],
                                      timeout=CFG.timeout_upgrade,
                                      on_line=print, cfg=CFG)
    if t.startswith("flatpak"):
        user = rec.extra.get("installation") == "user"
        argv = ["flatpak", "update", "-y"] + (["--user"] if user else []) + [n]
        if user:                      # 用户级安装不需要 root
            return actions.run_plain(argv, timeout=CFG.timeout_upgrade, cfg=CFG)
        return actions.run_privileged(argv, timeout=CFG.timeout_upgrade,
                                      on_line=print, cfg=CFG)
    # pip / appimage / 散落文件：pip 包属于某个具体环境，用当前解释器去升级会装错地方
    print(labels.update_advice(rec))
    return None


def _fail(res):
    _err(f"失败：{res.error}")
    if res.output:
        _err(res.output[-2000:])
    if res.command:
        _err("可手动执行：" + " ".join(res.command))
    return 1


def cmd_remove(args):
    inv = _collect(args)
    rec = _resolve(inv, args.name, args.type)
    if rec is None:
        return 1
    plan = preview(rec, purge_residues=args.purge_residues,
                   autoremove=not args.no_autoremove, cfg=CFG)
    if not plan.ok:
        _err(f"拒绝卸载 {rec.name}：{plan.error}")
        return 2

    print(f"卸载 {rec.name}  [{labels.class_text(rec.pkg_class)}] {rec.class_reason}")
    print(f"将移除 {len(plan.will_remove)} 个包:")
    for p in plan.will_remove[:40]:
        print(f"  {p}")
    if len(plan.will_remove) > 40:
        print(f"  ... 其余 {len(plan.will_remove) - 40} 个")
    if plan.residues:
        print(f"\n将删除 {len(plan.residues)} 处配置/缓存残留（共 {plan.freed_mb} MB）:")
        for path, size in plan.residues:
            print(f"  {path}  {size} MB")
    elif args.purge_residues:
        print("\n未检测到配置/缓存/数据残留")
    if len(plan.will_remove) > 1 and not args.purge_residues:
        print("\n注意：以上包会被一并移除（依赖关系导致）。")
    print(f"\n等效命令:\n  {plan.command_text or '（无需特权操作）'}")

    if not args.yes and not _confirm("\n确认卸载? [y/N] "):
        _err("已取消")
        return 1
    res = execute(plan, on_line=print, cfg=CFG)
    if res.ok:
        removed = [p for p in res.file.split(",") if p]
        print(f"\n✓ 卸载完成" + (f"，已删除 {len(removed)} 处残留" if removed else ""))
        return 0
    return _fail(res)


def cmd_config(args):
    out = []
    for k, v in sorted(CFG.dump().items()):
        out.append(f"  {report.pad(k, 26)}{v}")
    return _write("\n".join(out))


# ---------- 参数定义 ----------


def build_parser():
    ap = argparse.ArgumentParser(
        prog="pkgtool",
        description="本地软件包盘点与管理（deb 为主线，兼探 snap/flatpak/appimage/pip）",
        epilog="路径与阈值可用环境变量覆盖，见 `pkgtool config`。")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_filters(p):
        p.add_argument("-t", "--type", choices=_TYPES + ("all",), default="all",
                       help="只看某种包格式（同时会跳过其他后端的采集）")
        p.add_argument("-q", "--query", default="", help="按名字/版本/来源/路径过滤")
        p.add_argument("--local", action="store_true", help="只看用户自己装的")
        p.add_argument("--all", action="store_true",
                       help="连系统预装与 apt 自动依赖一起显示（默认隐藏）")
        p.add_argument("--removable", action="store_true", help="只看判定为“用户软件”、允许卸载的")
        p.add_argument("--upgradable", action="store_true", help="只看有新版本可升级的")
        p.add_argument("--loose", action="store_true", help="只看磁盘上散落的包文件")
        p.add_argument("--env", default="", help="只看某个 Python 环境（见 `pkgtool envs`）")
        p.add_argument("--check-updates", action="store_true",
                       help="联网查 flathub 新版（约 2 秒；deb 的可升级检测本来就离线）")
        p.add_argument("--quiet", action="store_true", help="不打印采集进度")

    p = sub.add_parser("list", help="列出已安装的包（默认视图）")
    add_filters(p)
    p.add_argument("--names-only", action="store_true", help="只输出包名，便于管道")
    p.add_argument("-f", "--format", choices=("table", "csv", "json"), default="table")
    p.add_argument("-o", "--output", default="", help="写入文件而不是 stdout")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("summary", help="按格式/通道/来源/类别汇总统计")
    p.add_argument("--top", type=int, default=15, help="每个分组最多列几项（默认 15）")
    p.add_argument("-o", "--output", default="")
    p.add_argument("--check-updates", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("info", help="单个包的详情：来源、通道、文件、更新方式、能否卸载")
    p.add_argument("name")
    p.add_argument("-t", "--type", default=None, help="同名多类型时指定")
    p.add_argument("--check-updates", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("envs", help="探测到的 Python 环境与各自包数")
    p.set_defaults(func=cmd_envs)

    p = sub.add_parser("search", help="在已配置的 apt 源里搜索（离线，读本地索引）")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="某个包在 apt 源里的全部候选版本")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("download", help="只下载 .deb 不安装（无需 root）")
    p.add_argument("name")
    p.add_argument("-v", "--version", default="", help="指定版本，默认仓库最新版")
    p.add_argument("-d", "--dest", default="", help=f"保存目录（默认 {CFG.download_dir}）")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("upgrade", help="升级指定包；不带参数则列出可升级项")
    p.add_argument("names", nargs="*")
    p.add_argument("--all", action="store_true", help="apt-get upgrade 全部升级")
    p.add_argument("-t", "--type", default=None)
    p.add_argument("--check-updates", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("remove", help="卸载（先 dry-run 预览，确认后才执行）")
    p.add_argument("name")
    p.add_argument("-t", "--type", default=None)
    p.add_argument("--purge-residues", action="store_true",
                   help="连主目录下的配置/缓存/数据残留一起删（默认不删）")
    p.add_argument("--no-autoremove", action="store_true", help="不清理变成孤儿的依赖")
    p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p.add_argument("--check-updates", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("config", help="打印当前生效的全部路径与阈值")
    p.set_defaults(func=cmd_config)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        _err("\n已中断")
        return 130
    except BrokenPipeError:        # pkgtool list | head 一类
        return 0
    except OSError as e:
        _err(f"错误：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
