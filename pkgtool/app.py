"""pkgtool.app — 交互式主界面（裸跑 `pkgtool` 进入）。

七个视图，Tab 或数字键切换：
  1 全部包  2 本地自装  3 可升级  4 散落包文件  5 磁盘回收  6 apt 搜索  7 Python 环境

本文件只有界面与状态机，业务规则一律走既有各层：inventory 取数、classify 判定、
remove / clean / upgrade / actions 执行、labels / report 出文案。这样交互界面和
子命令永远给出同一套结论——原 Web UI 就是因为自己重算了一遍分类和通道，
才和 CLI 对同一份数据给出不同答案。

所有会产生子进程的动作都通过 Screen.run_external 让出终端执行：sudo 密码提示
和命令输出需要真实终端，跟 curses 画面混在一起会花屏。
"""
import curses
import locale
import sys
from dataclasses import dataclass

from . import clean, classify, inventory, labels, remove, report, tui, upgrade
from .apt import actions, lists
from .backends import pip as pip_backend
from .config import CFG
from .report import pad, truncate

VIEWS = ("全部包", "本地自装", "可升级", "散落包文件", "磁盘回收",
         "apt 搜索", "Python 环境")
_LIST, _LOCAL, _UPGRADABLE, _LOOSE, _CLEAN, _SEARCH, _ENV = range(len(VIEWS))
_SEARCH_LIMIT = 80

_HELP = """按键一览

  浏览
    ↑ ↓ / k j         上下移动          PgUp PgDn   翻页
    g  G              跳到开头 / 结尾    Tab / 1-7   切换视图
    /                 编辑过滤词（apt 搜索视图里就是搜索词）
    s                 显示 / 隐藏系统预装与自动依赖
    R                 重新采集           ?           本帮助
    q 或 Esc          退出（详情里是返回）

  对选中的包
    Enter             查看详情
    r                 卸载（先 dry-run 预览，确认后才执行）
    u                 升级
    d                 下载 .deb（仅 deb，不需要 root）

  磁盘回收视图
    Enter             删除当前项        空格        标记 / 取消标记
    x                 删除全部标记项     t           切换「移入回收站 / 真删」

  apt 搜索视图
    Enter             看该包的全部候选版本
    i                 安装 / 升级到候选版本      d   只下载 .deb

说明：需要 root 的动作由 sudo 在终端上直接提示密码，本程序不经手密码。
      卸载与清理都会先打印计划再确认；磁盘回收默认真删，按 t 可改成移入回收站。
"""


@dataclass
class Item:
    kind: str                 # pkg / target / repo / env / hint
    head: str
    sub: str
    data: object
    color: str = "norm"
    marked: bool = False


def _render(it, selected):
    mark = f"{'*' if it.marked else ' '}{'>' if selected else ' '}"
    color = "sel" if selected else it.color
    return [(f"{mark} {it.head}", color), (it.sub, "dim")]


def _ask_yes(prompt, default=False):
    try:
        a = input(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    except EOFError:
        return default
    return default if not a else a in ("y", "yes")


def _pause(text="按 Enter 返回界面…"):
    try:
        input(text)
    except (EOFError, KeyboardInterrupt):
        pass


class App:
    def __init__(self, cfg, inv):
        self.cfg = cfg
        self.inv = inv
        self.view = _LIST
        self.filter = {i: "" for i in range(len(VIEWS))}
        self.show_system = False
        self.env = ""
        self.trash = False
        self.items = []
        self.lv = tui.ListView([], _render, per_item=2)
        self.mode = "list"               # list / detail
        self.detail_lines = []
        self.detail_rec = None           # 详情对应的记录（供 r/u/d）
        self.detail_repo = None          # 详情对应的仓库包名（供 i/d）
        self.scroll = 0
        self.msg = "按 ? 看帮助"
        self._index = None
        self.rebuild()

    # ---------- 数据 ----------

    @property
    def q(self):
        return self.filter[self.view].strip()

    def index(self):
        if self._index is None:
            self._index = lists.load_index(self.cfg)
        return self._index

    def rebuild(self):
        builders = (self._pkgs_all, self._pkgs_local, self._pkgs_upgradable,
                    self._loose, self._targets, self._repo_search, self._envs)
        self.items = builders[self.view]()
        self.lv.set_items(self.items)

    def current(self):
        return self.lv.current

    def _pkg_item(self, r):
        up = " ↑" if r.upgradable else ""
        head = (pad(r.pkg_type, 10) + pad(truncate(r.name, 30), 32)
                + pad(truncate(r.version + up, 24), 26)
                + pad(labels.channel_short(r.channel), 10)
                + pad(labels.class_text(r.pkg_class), 9)
                + labels.origin_short(r))
        bits = []
        if r.first_install:
            bits.append("首装 " + r.first_install[:10])
        if r.install_path:
            bits.append(truncate(r.install_path, 34))
        if r.executables:
            bits.append(f"{len(r.executables)} 个可执行")
        if r.extra.get("env"):
            bits.append("env " + r.extra["env"])
        if r.upgradable:
            bits.append("→ " + r.candidate)
        color = "warn" if r.is_loose_file else ("ok" if r.upgradable else "norm")
        return Item("pkg", head, " · ".join(bits), r, color)

    def _pkgs_all(self):
        return [self._pkg_item(r) for r in inventory.select(
            self.inv, show_system=self.show_system, query=self.q,
            env=self.env or None)]

    def _pkgs_local(self):
        return [self._pkg_item(r) for r in inventory.select(
            self.inv, only_local=True, show_system=self.show_system,
            query=self.q, env=self.env or None)]

    def _pkgs_upgradable(self):
        return [self._pkg_item(r) for r in inventory.select(
            self.inv, only_upgradable=True, query=self.q)]

    def _loose(self):
        recs = inventory.select(self.inv, loose=True, query=self.q)
        recs.sort(key=lambda r: -r.extra.get("size_mb", 0))
        return [Item("pkg",
                     pad(truncate(r.name, 30), 32)
                     + pad(truncate(r.version, 24), 26)
                     + pad(f"{r.extra.get('size_mb', 0)} MB", 11)
                     + labels.loose_state(r),
                     r.extra.get("loose", ""), r, "warn") for r in recs]

    def _targets(self):
        q = self.q.lower()
        out = []
        for t in clean.collect_targets(self.cfg, inv=self.inv):
            if q and q not in f"{t.kind} {t.label} {t.detail}".lower():
                continue
            how = " ".join(t.argv) if t.argv else "删除文件"
            out.append(Item("target",
                            pad(t.size_text, 10) + "  "
                            + truncate(labels.clean_label(t.kind, t.label), 58),
                            " · ".join(x for x in (t.detail, how, t.note) if x),
                            t, "warn" if t.privileged else "norm"))
        return out

    def _repo_search(self):
        if not self.q:
            return [Item("hint", "按 / 输入关键词，搜索所有已配置的 apt 源",
                         "", None, "dim")]
        hits = self.index().search(self.q, _SEARCH_LIMIT)
        if not hits:
            return [Item("hint", f"未找到与 “{self.q}” 相关的包", "", None, "dim")]
        return [Item("repo",
                     pad(truncate(x["name"], 34), 36)
                     + pad(truncate(x["version"], 24), 26)
                     + pad(f"{x['size_kb']}K", 9)
                     + truncate(x["repo"] or "", 26),
                     truncate(x.get("desc") or "", 110), x) for x in hits]

    def _envs(self):
        out = []
        for e in pip_backend.environment_summary(self.cfg):
            empty = e.get("empty")
            out.append(Item("env",
                            pad(truncate(e["label"], 50), 52)
                            + pad(str(e["packages"]), 8),
                            "空壳（没装 python）" if empty else "Enter 查看该环境里的包",
                            e, "dim" if empty else "norm"))
        return out or [Item("hint", "未探测到 Python 环境", "", None, "dim")]

    # ---------- 状态变更 ----------

    def set_view(self, v):
        if v != self.view:
            self.view = v
            self.mode = "list"
            self.rebuild()

    def reload(self, scr):
        def go():
            print("\n重新采集中…")
            self.inv = inventory.collect(
                self.cfg, on_backend=lambda t, n, e, err: print(
                    f"  [{t}] {n} 个 ({e:.1f}s)" + (f"  失败: {err}" if err else "")))
            self._index = None
        scr.run_external(go)
        self.env = ""
        self.rebuild()
        self.msg = f"已重新采集 {len(self.inv.records)} 条 @ {self.inv.collected_at}"

    def _drop_names(self, names):
        """卸载成功后就地摘掉记录，省一次全量重采。"""
        names = set(names)
        self.inv.records = [r for r in self.inv.records if r.name not in names]
        self.inv.by_key = {k: v for k, v in self.inv.by_key.items()
                           if v.name not in names}

    def _bump(self, rec):
        if rec.candidate:
            rec.version, rec.candidate = rec.candidate, ""

    # ---------- 渲染 ----------

    def draw(self, scr):
        h, w = scr.h, scr.w
        scr.clear()
        if h < 10 or w < 60:
            scr.text(0, 0, "终端窗口太小，请放大到至少 10 行 60 列")
            scr.refresh()
            return
        scr.text(0, 0, truncate(" pkgtool · " + self._tabs(), w - 1), "title")
        scr.text(1, 0, truncate(" " + " · ".join(self._status_bits()), w - 1), "dim")
        body_top, body_h = 2, h - 5
        if self.mode == "detail":
            self._draw_detail(scr, body_top, body_h, w)
        else:
            self.lv.per_item = 2 if body_h >= 12 else 1
            if not self.items:
                scr.text(body_top + 1, 2, "（没有匹配的条目）", "dim")
            else:
                self.lv.draw(scr, body_top, body_h)
        scr.hline(h - 3)
        scr.text(h - 2, 0, " " + truncate(self._hints(), w - 2), "title")
        scr.text(h - 1, 0, " " + truncate(self.msg, w - 2), "dim")
        scr.refresh()

    def _tabs(self):
        return " ".join(f"[{i}]{n}" if i - 1 == self.view else f" {i} {n} "
                        for i, n in enumerate(VIEWS, start=1))

    def _status_bits(self):
        bits = [f"{len(self.items)} 项"]
        if self.q:
            bits.append(f"过滤 “{self.q}”")
        if self.env:
            bits.append("env=" + self.env)
        if self.view in (_LIST, _LOCAL) and self.show_system:
            bits.append("含系统组件")
        if self.view == _CLEAN:
            bits.append("回收站模式" if self.trash else "真删模式")
        return bits

    def _draw_detail(self, scr, y0, body_h, w):
        n = len(self.detail_lines)
        self.scroll = max(0, min(self.scroll, max(0, n - body_h)))
        for i in range(body_h):
            j = self.scroll + i
            if j >= n:
                break
            scr.text(y0 + i, 0, truncate(self.detail_lines[j], w - 1),
                     "title" if j == 0 else None)
        if n > body_h:
            scr.text(y0, max(0, w - 14), f" {self.scroll + 1}/{n} ", "dim")

    def _hints(self):
        if self.mode == "detail":
            if self.detail_repo:
                return "i 安装/升级 · d 下载 .deb · ↑↓ 滚动 · q 返回列表"
            return "r 卸载 · u 升级 · d 下载 .deb · ↑↓ 滚动 · q 返回列表"
        if self.view == _CLEAN:
            return ("Enter 删除 · 空格 标记 · x 删标记 · t 切回收站 · / 过滤 · "
                    "Tab 换视图 · ? 帮助 · q 退出")
        return ("Enter 详情 · r 卸载 · u 升级 · d 下载 · / 过滤 · s 含系统 · "
                "R 重采 · Tab 换视图 · ? 帮助 · q 退出")

    # ---------- 详情 ----------

    def open_detail(self):
        it = self.current()
        if it is None or it.data is None:
            return
        if it.kind == "pkg":
            self.detail_rec, self.detail_repo = it.data, None
            self.detail_lines = report.render_detail(it.data).splitlines()
        elif it.kind == "repo":
            self.detail_rec, self.detail_repo = None, it.data["name"]
            info = self.index().info(it.data["name"])
            self.detail_lines = (report.render_versions(info).splitlines()
                                 if info else ["仓库中无此包"])
        elif it.kind == "env":
            self.env = it.data["label"]
            self.filter[_LIST] = ""
            self.set_view(_LIST)
            self.rebuild()
            self.msg = f"已按环境过滤：{self.env}"
            return
        self.scroll = 0
        self.mode = "detail"

    def show_help(self):
        self.detail_rec = self.detail_repo = None
        self.detail_lines = _HELP.splitlines()
        self.scroll = 0
        self.mode = "detail"

    def close_detail(self):
        self.mode = "list"
        self.detail_rec = self.detail_repo = None

    # ---------- 动作（都在真实终端上执行）----------

    def act_remove(self, scr, rec):
        if not classify.is_removable(rec):
            self.msg = f"不能卸载 {rec.name}：{labels.block_reason(rec.pkg_class)}"
            return
        box = {}

        def go():
            print()
            print(report.render_detail(rec))
            purge = _ask_yes("\n同时删除主目录下的配置/缓存残留?")
            plan = remove.preview(rec, purge_residues=purge, cfg=self.cfg)
            box["plan"] = plan
            if not plan.ok:
                print(f"\n拒绝卸载 {rec.name}：{plan.error}")
                _pause()
                return False
            print(f"\n将移除 {len(plan.will_remove)} 个包：")
            for p in plan.will_remove[:40]:
                print("  " + p)
            if len(plan.will_remove) > 40:
                print(f"  … 其余 {len(plan.will_remove) - 40} 个")
            if plan.residues:
                print(f"\n将删除 {len(plan.residues)} 处残留（{plan.freed_mb} MB）：")
                for p, s in plan.residues:
                    print(f"  {p}  {s} MB")
            print(f"\n等效命令：\n  {plan.command_text or '（无需特权）'}")
            if not _ask_yes(f"\n确认卸载 {rec.name}?"):
                print("已取消")
                _pause()
                return False
            res = remove.execute(plan, on_line=print, cfg=self.cfg)
            print("✓ 卸载完成" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self._drop_names(box["plan"].will_remove)
            self.close_detail()
            self.rebuild()
            self.msg = f"已卸载 {rec.name}"

    def act_upgrade(self, scr, rec):
        def go():
            print()
            if upgrade.plan(rec) is None:
                print(labels.update_advice(rec))
                _pause()
                return False
            print(f"升级 {rec.pkg_type} {rec.name} {rec.version}"
                  + (f" → {rec.candidate}" if rec.candidate else ""))
            if not _ask_yes("执行?"):
                return False
            res = upgrade.run(rec, on_line=print, cfg=self.cfg)
            print("✓ 完成" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self._bump(rec)
            self.close_detail()
            self.rebuild()
            self.msg = f"已升级 {rec.name}（按 R 重新采集可刷新全部数据）"

    def act_install(self, scr, name, version=""):
        """从 apt 源安装 / 升级到指定版本。"""
        def go():
            print(f"\n安装 {name} {version or '(候选版本)'}")
            if not _ask_yes("执行?"):
                return False
            res = actions.upgrade(name, version, on_line=print, cfg=self.cfg)
            print("✓ 完成" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self.close_detail()
            self.msg = f"已安装 {name}（按 R 重新采集以刷新列表）"

    def act_download(self, scr, name, version=""):
        def go():
            print(f"\n下载 {name} {version or '(候选版本)'} …")
            res = actions.download(name, version, cfg=self.cfg)
            print(f"✓ {res.file}" if res.ok else f"✗ {res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self.msg = f"已下载到 {self.cfg.download_dir}"

    def act_clean(self, scr, target):
        def go():
            print(f"\n→ {labels.clean_label(target.kind, target.label)}"
                  f"（{target.size_text}）")
            print("  " + target.detail)
            print("  方式: " + (" ".join(target.argv) if target.argv
                                else "删除 " + ", ".join(target.paths)))
            if not _ask_yes("执行?"):
                return False
            res = clean.delete(target, trash=self.trash, on_line=print, cfg=self.cfg)
            print("✓ 完成" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self.rebuild()
            self.msg = f"已清理 {labels.clean_label(target.kind, target.label)}"

    def act_clean_marked(self, scr):
        targets = [it.data for it in self.items if it.marked and it.kind == "target"]
        if not targets:
            self.msg = "没有标记项（先用空格标记）"
            return

        def go():
            print(f"\n将清理 {len(targets)} 项：")
            for t in targets:
                print(f"  {t.size_text:>9}  {labels.clean_label(t.kind, t.label)}")
            if not _ask_yes("\n全部执行?"):
                return 0
            ok = 0
            for t in targets:
                res = clean.delete(t, trash=self.trash, on_line=print, cfg=self.cfg)
                print(f"{'✓' if res.ok else '✗'} "
                      f"{labels.clean_label(t.kind, t.label)}: "
                      f"{res.error or t.size_text}")
                ok += 1 if res.ok else 0
            _pause()
            return ok

        n = scr.run_external(go)
        if n:
            self.rebuild()
            self.msg = f"已清理 {n} 项"

    def edit_filter(self, scr):
        label = "搜索 apt 源: " if self.view == _SEARCH else "过滤: "
        val = scr.prompt(label, self.filter[self.view])
        if val is None:
            self.msg = "已取消"
            return
        self.filter[self.view] = val
        self.rebuild()
        self.msg = (f"过滤 “{val}” → {len(self.items)} 项" if val
                    else "已清除过滤")


def _nav(app, kind, val):
    """列表导航。Tab 在 getch 下是 9 号字符，Shift-Tab 是 KEY_BTAB。"""
    if kind == "char":
        if val == "\t":
            app.set_view((app.view + 1) % len(VIEWS))
            return True
        if val in "1234567":
            app.set_view(int(val) - 1)
            return True
    if kind == "key" and val == curses.KEY_BTAB:
        app.set_view((app.view - 1) % len(VIEWS))
        return True
    return app.lv.handle_key(kind, val)


def _main(std, cfg, inv):
    scr = tui.Screen(std)
    app = App(cfg, inv)

    while True:
        app.draw(scr)
        kind, val = scr.read_key()

        if app.mode == "detail":
            if tui.is_quit(kind, val):
                app.close_detail()
            elif kind == "char" and val == "?":
                app.show_help()
            elif kind == "key" and val == curses.KEY_UP:
                app.scroll -= 1
            elif kind == "key" and val == curses.KEY_DOWN:
                app.scroll += 1
            elif kind == "char" and val == "k":
                app.scroll -= 1
            elif kind == "char" and val == "j":
                app.scroll += 1
            elif kind == "char" and val == "g":
                app.scroll = 0
            elif kind == "char" and val == "G":
                app.scroll = len(app.detail_lines)
            elif kind == "char" and val == "r" and app.detail_rec:
                app.act_remove(scr, app.detail_rec)
            elif kind == "char" and val == "u" and app.detail_rec:
                app.act_upgrade(scr, app.detail_rec)
            elif kind == "char" and val == "i" and app.detail_repo:
                info = app.index().info(app.detail_repo) or {}
                app.act_install(scr, app.detail_repo, info.get("candidate", ""))
            elif kind == "char" and val == "d":
                if app.detail_repo:
                    app.act_download(scr, app.detail_repo)
                elif app.detail_rec and app.detail_rec.pkg_type == "deb":
                    app.act_download(scr, app.detail_rec.name,
                                     app.detail_rec.candidate)
                else:
                    app.msg = "只有 deb 包能下载 .deb 文件"
            continue

        if tui.is_quit(kind, val):
            return 0
        if kind == "char" and val == "?":
            app.show_help()
            continue
        if kind == "char" and val == "/":
            app.edit_filter(scr)
            continue
        if kind == "char" and val == "s":
            app.show_system = not app.show_system
            app.rebuild()
            app.msg = ("显示系统预装与自动依赖" if app.show_system
                       else "隐藏系统预装与自动依赖")
            continue
        if kind == "char" and val == "R":
            app.reload(scr)
            continue
        if kind == "char" and val == "t" and app.view == _CLEAN:
            app.trash = not app.trash
            app.msg = ("删除方式：移入回收站（可还原）" if app.trash
                       else "删除方式：真删")
            continue
        if app.view == _CLEAN and kind == "char" and val == " ":
            it = app.current()
            if it and it.kind == "target":
                it.marked = not it.marked
                app.lv.move(1)
            continue
        if app.view == _CLEAN and kind == "char" and val == "x":
            app.act_clean_marked(scr)
            continue
        if tui.is_enter(kind, val):
            it = app.current()
            if it and it.kind == "target":
                app.act_clean(scr, it.data)
            elif it:
                app.open_detail()
            continue
        if kind == "char" and val == "r":
            it = app.current()
            if it and it.kind == "pkg" and not it.data.is_loose_file:
                app.act_remove(scr, it.data)
            elif it and it.kind == "pkg":
                app.msg = "散落文件请切到「磁盘回收」视图删除"
            continue
        if kind == "char" and val == "u":
            it = app.current()
            if it and it.kind == "pkg":
                app.act_upgrade(scr, it.data)
            continue
        if kind == "char" and val == "d":
            it = app.current()
            if it and it.kind == "repo":
                app.act_download(scr, it.data["name"], it.data["version"])
            elif it and it.kind == "pkg" and it.data.pkg_type == "deb":
                app.act_download(scr, it.data.name, it.data.candidate)
            else:
                app.msg = "只有 deb 包能下载 .deb 文件"
            continue
        if _nav(app, kind, val):
            continue
        app.msg = f"未识别的按键 {val!r}（按 ? 看帮助）"


def run(cfg=CFG, inv=None):
    """裸跑 pkgtool 的入口 → 退出码。非 tty 返回 None，由 CLI 降级为提示。"""
    if not tui.is_tty():
        return None
    if inv is None:
        inv = inventory.collect(
            cfg, on_backend=lambda t, n, e, err: print(
                f"[{t}] {n} 个 ({e:.1f}s)" + (f"  失败: {err}" if err else ""),
                file=sys.stderr))
    try:
        locale.setlocale(locale.LC_ALL, "")
        rc = curses.wrapper(_main, cfg, inv)
        return 0 if rc is None else rc
    except curses.error as e:
        print(f"无法进入交互界面（终端不支持 curses）：{e}", file=sys.stderr)
        return 1
