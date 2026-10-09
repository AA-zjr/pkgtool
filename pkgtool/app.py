"""pkgtool.app — 交互式主界面（裸跑 `pkgtool` 进入）。

六个视图，Tab 或数字键切换：
  1 全部包  2 可升级  3 散落包文件  4 仓库搜索  5 磁盘回收

本文件只有界面与状态机，业务规则一律走既有各层：inventory 取数、classify 判定、
remove / clean / upgrade / actions 执行、labels / report 出文案。这样交互界面和
子命令永远给出同一套结论——原 Web UI 就是因为自己重算了一遍分类和通道，
才和 CLI 对同一份数据给出不同答案。

所有会产生子进程的动作都通过 Screen.run_external 让出终端执行：sudo 密码提示
和命令输出需要真实终端，跟 curses 画面混在一起会花屏。
"""
import curses
import locale
import os
import sys
from dataclasses import dataclass

from . import (catalog, clean, classify, inventory, labels, launch, remove,
               report, tui, upgrade)
from .apt import actions, lists
from .config import CFG
from .report import pad, truncate

VIEWS = ("全部包", "可升级", "散落包文件", "仓库搜索", "磁盘回收")
_LIST, _UPGRADABLE, _LOOSE, _SEARCH, _CLEAN = range(len(VIEWS))
_SEARCH_LIMIT = 80
# t 键循环的包类型；flatpak 一项同时覆盖 flatpak 与 flatpak-runtime
_TYPE_CYCLE = ("all", "deb", "snap", "flatpak", "linyap", "appimage")
# 仓库搜索视图里 s 键循环的目录来源（见 catalog.py）
_SOURCE_CYCLE = ("all", "apt", "snap", "flatpak")
_SOURCE_LABEL = {"all": "全部来源", "apt": "apt 源", "snap": "Snap Store",
                 "flatpak": "flathub"}

_HELP = """按键一览

  浏览
    ↑ ↓ / k j         上下移动          PgUp PgDn   翻页
    g  G              跳到开头 / 结尾    Tab / 1-6   切换视图
    /                 搜索 / 过滤（apt 搜索视图里就是查询词）
    t                 包类型筛选：全部 → deb → snap → flatpak → appimage
    s                 显示层级：仅软件 → +依赖/数据 → 全部
    R                 重新采集           ?           本帮助
    q 或 Esc          退出（详情里是返回）

  对选中的包
    Enter             查看详情           r           卸载（散落文件则是删除，先确认）
    u                 升级               d           下载 .deb（仅 deb，不需 root）
    o                 启动应用（分离启动，不阻塞本界面）
    （r/o/u/d/T 只在适用的条目上出现；不适用的按键按了没反应）

  磁盘回收视图（散落文件、用户缓存 ~/.cache、flatpak 应用缓存、
  apt 下载缓存、snap 旧修订、flatpak 无用运行时、conda 包缓存）
    Enter             删除当前项         空格        标记 / 取消标记
    x                 删除全部标记项      T           切换「移入回收站 / 真删」
    v                 进入当前目录下层（逐层深入定位大缓存来源）
    b                 返回上一层

  仓库搜索视图（apt 源 / Snap Store / flathub 三源）
    /                 输入关键词         s           切换来源（全部/apt/snap/flatpak）
    Enter             详情：apt 列全部候选版本，snap 给 snap info，flatpak 给基本字段
    i                 安装：按来源自动选 apt-get / snap install / flatpak install
    d                 只下载 .deb（仅 apt 源）

  三个来源的差别（见 catalog.py）
    apt      读本地索引，离线、毫秒级，含描述与全部候选版本
    snap     联网查 Snap Store，约 2 秒，含发布者/摘要；classic 会自动补 --classic
    flatpak  读本地 remote 缓存，0.3 秒 / 3483 条，有体积但**没有描述**
             （描述在 appstream 缓存里，本机没有；flatpak 官方的 search 命令
              在这种情况下会联网拉取并挂死，所以不用它）

「大小」列的含义
    12.3 MB           包自身的已安装体积
    2.1 GB (14)       自身 + 14 个「独占依赖」的合计。独占依赖指只被这一个包
                      硬依赖、且 apt 标记为 auto 的包（取传递闭包）——删掉这个包
                      它们就没用了。库被多个包共用时不计入，所以微信、clash-verge
                      这类应用通常显示 0 个独占依赖。
                      注意这与卸载时实际释放的空间不是一回事：卸载预览走的是
                      apt 的 --autoremove 模拟，含反向依赖级联，数字通常更大。

说明：需要 root 的动作由 sudo 在终端上直接提示密码，本程序不经手密码。
      卸载与清理都会先打印计划再确认；磁盘回收默认真删，按 T 可改成移入回收站。
      散落包文件视图里 r 直接删除（主目录内默认真删，按 T 可改成移入回收站）。
"""


@dataclass
class Item:
    kind: str                 # pkg / target / repo / hint
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
        self.level = 0                  # 披露层级：0 仅软件 → 1 +依赖 → 2 全部
        self.ptype = "all"             # 包类型筛选，t 键循环
        self.source = "all"            # 仓库搜索的来源筛选，s 键循环
        self.trash = False
        self.drill = None               # 磁盘回收下钻：当前所在的缓存目录路径
        self.items = []
        self.lv = tui.ListView([], _render, per_item=2)
        self.mode = "list"               # list / detail
        self.detail_lines = []
        self.detail_rec = None           # 详情对应的记录（供 r/u/d）
        self.detail_item = None          # 详情对应的仓库包名（供 i/d）
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
        builders = (self._pkgs_all, self._pkgs_upgradable,
                    self._loose, self._repo_search, self._targets)
        self.items = builders[self.view]()
        self.lv.set_items(self.items)

    def current(self):
        return self.lv.current

    def _sel_rec(self):
        it = self.current()
        if it and it.kind == "pkg" and it.data is not None:
            return it.data
        return None

    def _rec_hints(self, rec, detail=False):
        """按选中记录的实际能力出提示：不适用的按键不显示（按了也没反应），
        而不是摆在那里等用户按了吃报错。"""
        bits = []
        if rec.is_loose_file:
            bits.append("r 删除")
        elif classify.is_removable(rec):
            bits.append("r 卸载")
        if launch.plan(rec):
            bits.append("o 启动")
        if not rec.is_loose_file and upgrade.plan(rec):
            bits.append("u 升级")
        if rec.pkg_type == "deb" and not rec.is_loose_file:
            bits.append("d 下载")
        if rec.is_loose_file:
            bits.append("T 回收站")
        if detail:
            bits += ["↑↓ 滚动", "q 返回列表"]
        return bits

    def _pkg_item(self, r):
        up = " ↑" if r.upgradable else ""
        # 大小列右对齐后必须补一个空格，否则会和「通道」列粘成 "6.5 MBapt"
        head = (pad(r.pkg_type, 10) + pad(truncate(r.name, 30), 32)
                + pad(truncate(r.version + up, 22), 24)
                + pad(labels.size_pair_text(r), 13, ">") + " "
                + pad(labels.channel_short(r.channel), 10)
                + pad(labels.class_text(r.pkg_class), 9)
                + pad((r.first_install or "—")[:10], 10))
        bits = []
        if r.origin_repos:
            bits.append(labels.origin_short(r))
        if r.install_path:
            bits.append(truncate(r.install_path, 30))
        if r.exclusive_deps:
            bits.append(f"独占依赖 {len(r.exclusive_deps)} 个 "
                        f"{labels.size_text(r.deps_size_mb)}")
        if r.executables:
            bits.append(f"{len(r.executables)} 个可执行")
        if r.upgradable:
            bits.append("→ " + r.candidate)
        color = "warn" if r.is_loose_file else ("ok" if r.upgradable else "norm")
        return Item("pkg", head, " · ".join(bits), r, color)

    def _pkgs_all(self):
        return [self._pkg_item(r) for r in inventory.select(
            self.inv, level=self.level, query=self.q, pkg_type=self.ptype)]

    def _pkgs_upgradable(self):
        return [self._pkg_item(r) for r in inventory.select(
            self.inv, only_upgradable=True, level=self.level,
            query=self.q, pkg_type=self.ptype)]

    def _loose(self):
        recs = inventory.select(self.inv, loose=True, query=self.q,
                                pkg_type=self.ptype)
        recs.sort(key=lambda r: -r.size_mb)
        return [Item("pkg",
                     pad(truncate(r.name, 30), 32)
                     + pad(truncate(r.version, 24), 26)
                     + pad(labels.size_text(r.size_mb), 11)
                     + labels.loose_state(r),
                     r.extra.get("loose", ""), r, "warn") for r in recs]

    def _targets(self):
        q = self.q.lower()
        if self.drill:
            # 下钻模式：只列当前目录的下一层子项，可继续 v 逐层深入
            out = []
            for t in clean.child_targets(self.drill, cfg=self.cfg):
                if q and q not in f"{t.kind} {t.label} {t.detail}".lower():
                    continue
                out.append(Item("target",
                                pad(t.size_text, 10) + "  "
                                + truncate(truncate(t.label, 40), 58),
                                t.detail, t, "norm"))
            if not out:
                out = [Item("target", "（没有可清理的子项）",
                            "b 返回上一层", None, "dim")]
            return out
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
            return [Item("hint",
                         "按 / 输入关键词，搜 apt 源 / Snap Store / flathub"
                         "（s 键切换来源）", "", None, "dim")]
        hits = catalog.search(self.q,
                              sources=None if self.source == "all" else (self.source,),
                              limit=_SEARCH_LIMIT, cfg=self.cfg,
                              installed=catalog.installed_keys(self.inv))
        if not hits:
            note = ("：" + "；".join(catalog.LAST_ERRORS)) if catalog.LAST_ERRORS else ""
            return [Item("hint", f"未找到与 “{self.q}” 相关的条目{note}",
                         "", None, "dim")]
        out = []
        for x in hits:
            # apt / snap 的显示名就是安装标识，重复占两列纯浪费宽度；
            # flatpak 才有独立的显示名（Browser vs com.brave.Browser）
            ident = "" if x.name == x.title else x.name
            head = (pad(x.source, 8) + pad("已装" if x.installed else "", 5)
                    + pad(truncate(x.title, 24), 26)
                    + pad(truncate(ident, 28), 30)
                    + pad(truncate(x.version or "—", 14), 16)
                    + pad(x.size_text or "—", 9, ">"))
            sub = " · ".join(t for t in (x.publisher, x.channel, x.summary) if t)
            out.append(Item("repo", head, truncate(sub, 120), x,
                            "ok" if x.installed else "norm"))
        return out

    # ---------- 状态变更 ----------

    def cycle_type(self):
        """t 键循环包类型筛选：全部 → deb → snap → flatpak → appimage。
        直接走 inventory.select 的 pkg_type 参数，不另写一套过滤逻辑，
        这样和 `pkgtool list -t xxx` 的结果永远一致。"""
        i = _TYPE_CYCLE.index(self.ptype) if self.ptype in _TYPE_CYCLE else 0
        self.ptype = _TYPE_CYCLE[(i + 1) % len(_TYPE_CYCLE)]
        self.rebuild()
        name = ("全部类型" if self.ptype == "all"
                else labels.PKG_TYPE_LABEL.get(self.ptype, self.ptype))
        self.msg = f"包类型：{name} → {len(self.items)} 项"

    def cycle_level(self):
        """s 键循环披露层级：仅软件 → +依赖/数据 → 全部。
        直接走 inventory.select 的 level 参数，与 `pkgtool list` 的 --all
        共用一套过滤（--all 即层级 2）。"""
        self.level = (self.level + 1) % 3
        self.rebuild()
        name = {0: "仅软件", 1: "软件 + 库/数据", 2: "全部（含系统/基础）"}[self.level]
        self.msg = f"显示层级：{name} → {len(self.items)} 项"

    def cycle_source(self):
        """s 键（仅在仓库搜索视图）循环目录来源。
        snap 要联网约 2 秒、flatpak 首次要拉全量清单，能只搜一个源就只搜一个。"""
        i = _SOURCE_CYCLE.index(self.source) if self.source in _SOURCE_CYCLE else 0
        self.source = _SOURCE_CYCLE[(i + 1) % len(_SOURCE_CYCLE)]
        self.rebuild()
        el = " · ".join(f"{k} {v}s" for k, v in catalog.LAST_ELAPSED.items())
        self.msg = (f"来源：{_SOURCE_LABEL[self.source]} → {len(self.items)} 条"
                    + (f"（{el}）" if el else ""))

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
        self.rebuild()
        self.msg = f"已重新采集 {len(self.inv.records)} 条 @ {self.inv.collected_at}"

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
            head = self._header()
            if head:
                scr.text(body_top, 3, truncate(head, w - 4), "dim")
                body_top += 1
                body_h -= 1
            self.lv.per_item = 2 if body_h >= 12 else 1
            if not self.items:
                scr.text(body_top + 1, 2, "（没有匹配的条目）", "dim")
            else:
                self.lv.draw(scr, body_top, body_h)
        scr.hline(h - 3)
        scr.text(h - 2, 0, " " + truncate(self._hints(), w - 2), "title")
        scr.text(h - 1, 0, " " + truncate(self.msg, w - 2), "dim")
        scr.refresh()

    def _header(self):
        """列表视图的列头，列宽与对应的 _pkg_item / _repo_search 一一对应
        （改一处必须改另一处）。其余视图各自排版，不加列头。"""
        if self.view in (_LIST, _UPGRADABLE):
            return (pad("类型", 10) + pad("名称", 32) + pad("版本", 24)
                    + pad("大小", 13, ">") + " " + pad("通道", 10)
                    + pad("类别", 9) + pad("首次安装", 10))
        if self.view == _SEARCH and self.q:
            return (pad("来源", 8) + pad("", 5) + pad("名称", 26)
                    + pad("安装标识", 30) + pad("版本", 16)
                    + pad("体积", 9, ">"))
        return None

    def _tabs(self):
        return " ".join(f"[{i}]{n}" if i - 1 == self.view else f" {i} {n} "
                        for i, n in enumerate(VIEWS, start=1))

    def _status_bits(self):
        bits = [f"{len(self.items)} 项"]
        sized = sum(i.data.size_mb for i in self.items
                    if i.kind == "pkg" and i.data is not None)
        if sized:
            bits.append("体积 " + labels.size_text(round(sized, 1)))
        if self.q:
            bits.append(f"过滤 “{self.q}”")
        if self.ptype != "all":
            bits.append("类型 " + labels.PKG_TYPE_LABEL.get(self.ptype, self.ptype))
        if self.view == _LIST and self.level:
            bits.append({1: "软件+依赖", 2: "全部层级"}[self.level])
        if self.view == _SEARCH:
            bits.append("来源 " + _SOURCE_LABEL[self.source])
            el = " ".join(f"{k}{v}s" for k, v in catalog.LAST_ELAPSED.items())
            if el:
                bits.append(el)
        if self.view == _CLEAN:
            sized = sum(i.data.size_mb for i in self.items
                        if i.kind == "target" and i.data is not None
                        and i.data.size_known)
            if sized:
                bits.append("可回收 " + labels.size_text(round(sized, 1)))
            if self.drill:
                bits.append("位置 " + self.drill)
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
            if self.detail_item:
                bits = ["i 安装"]
                if self.detail_item.source == "apt":
                    bits.append("d 下载")
                bits += ["↑↓ 滚动", "q 返回列表"]
                return " · ".join(bits)
            if self.detail_rec:
                return " · ".join(self._rec_hints(self.detail_rec, detail=True))
            return "↑↓ 滚动 · q 返回列表"
        if self.view == _CLEAN:
            return ("Enter 删除 · v 下钻 · b 返回 · 空格 标记 · x 删标记 · "
                    "T 切回收站 · / 过滤 · ? 帮助 · q 退出")
        if self.view == _SEARCH:
            bits = ["/ 搜索", "s 切来源"]
            it = self.current()
            if it and it.kind == "repo":
                bits += ["Enter 详情", "i 安装"]
                if it.data.source == "apt":
                    bits.append("d 下载")
            bits += ["? 帮助", "q 退出"]
            return " · ".join(bits)
        if self.view in (_LIST, _UPGRADABLE, _LOOSE):
            bits = ["Enter 详情"]
            rec = self._sel_rec()
            if rec:
                bits += self._rec_hints(rec)
            bits += ["/ 搜索", "t 筛选"]
            if self.view == _LIST:
                bits.append("s 层级")
            bits += ["R 重采", "? 帮助", "q 退出"]
            return " · ".join(bits)
        return "? 帮助 · q 退出"

    # ---------- 详情 ----------

    def open_detail(self):
        it = self.current()
        if it is None or it.data is None:
            return
        if it.kind == "pkg":
            self.detail_rec, self.detail_item = it.data, None
            self.detail_lines = report.render_detail(it.data).splitlines()
        elif it.kind == "repo":
            self.detail_rec, self.detail_item = None, it.data
            self.detail_lines = catalog.describe(it.data, self.cfg).splitlines()
        self.scroll = 0
        self.mode = "detail"

    def show_help(self):
        self.detail_rec = self.detail_item = None
        self.detail_lines = _HELP.splitlines()
        self.scroll = 0
        self.mode = "detail"

    def close_detail(self):
        self.mode = "list"
        self.detail_rec = self.detail_item = None

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
            print("✓ 卸载完成" if res.ok else f"✗ 命令报错：{res.error}")
            # 卸载后立即重采：flatpak 偶发"命令报错但实际已卸掉"，就地摘记录
            # 不可靠，重采结果才是事实。本来就已在终端模式里，顺手做掉。
            print("\n重新采集中…")
            box["inv"] = inventory.collect(
                self.cfg, on_backend=lambda t, n, e, err: print(
                    f"  [{t}] {n} 个 ({e:.1f}s)" + (f"  失败: {err}" if err else "")))
            return True

        out = scr.run_external(go)
        if out:
            res_ok, gone = True, True
            if "inv" in box:
                new_inv = box["inv"]
                self.inv = new_inv
                gone = new_inv.by_key.get(rec.key) is None
            self.close_detail()
            self.rebuild()
            self.msg = (f"已卸载 {rec.name}" if res_ok else
                        f"{rec.name} 卸载命令报错，已重采核实："
                        + ("确认已卸载" if gone else "记录仍在，未卸载"))

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

    def act_open(self, rec):
        """启动应用。launch 是分离启动（Popen 后立即返回），不需要让出终端。"""
        res = launch.launch(rec, cfg=self.cfg)
        if res.ok:
            self.msg = "已启动: " + " ".join(res.command)
        else:
            self.msg = f"无法启动 {rec.name}：{res.error}"

    def act_delete_loose(self, scr, rec):
        """在散落包文件视图里就地删除，不必切到磁盘回收视图。
        删除逻辑复用 clean 层（普通权限回收站/真删，越权的走 sudo），
        保证和「磁盘回收」视图对同一文件的行为一致。"""
        t = clean.loose_target(rec, self.cfg)
        if t is None:
            self.msg = "文件已不在原位置（按 R 重新采集可刷新列表）"
            return

        def go():
            print(f"\n→ 删除散落文件：{t.detail}（{t.size_text}）")
            print("  方式：" + ("移入回收站" if self.trash and not t.privileged
                              else "真删" if not t.privileged else "sudo rm（文件在系统目录）"))
            if not _ask_yes("确认删除?"):
                print("已取消")
                _pause()
                return False
            res = clean.delete(t, trash=self.trash, on_line=print, cfg=self.cfg)
            print("✓ 已删除" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self.inv.records = [r for r in self.inv.records if r.key != rec.key]
            self.inv.by_key.pop(rec.key, None)
            self.rebuild()
            self.msg = f"已删除 {t.detail}"

    def act_install_item(self, scr, item):
        """从目录安装（apt / snap / flatpak 三源统一入口）。
        命令由 catalog.install_argv 生成：snap 的 classic confinement 必须带
        --classic，flatpak 要带上 remote 名，这些差异都收在 catalog 里。"""
        def go():
            argv, err = catalog.install_argv(item)
            if argv is None:
                print(f"拒绝安装：{err}")
                _pause()
                return False
            print(f"\n安装 [{item.source}] {item.title}（{item.name}）")
            if item.version:
                print(f"  版本: {item.version}")
            if item.size_text:
                print(f"  体积: {item.size_text}")
            print("  命令: " + " ".join(argv))
            if item.installed:
                print("  注意：本机已装过同名包，这会变成升级/覆盖安装")
            if not _ask_yes("执行?"):
                return False
            res = catalog.install(item, on_line=print, cfg=self.cfg)
            print("✓ 完成" if res.ok else f"✗ 失败：{res.error}")
            _pause()
            return res.ok

        if scr.run_external(go):
            self.close_detail()
            self.msg = f"已安装 {item.name}（按 R 重新采集以刷新列表）"

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
        if val in "123456789"[:len(VIEWS)]:
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
            elif kind == "char" and val == "o" and app.detail_rec \
                    and launch.plan(app.detail_rec):
                app.act_open(app.detail_rec)
            elif kind == "char" and val == "r" and app.detail_rec:
                if app.detail_rec.is_loose_file:
                    app.act_delete_loose(scr, app.detail_rec)
                elif classify.is_removable(app.detail_rec):
                    app.act_remove(scr, app.detail_rec)
            elif kind == "char" and val == "u" and app.detail_rec \
                    and not app.detail_rec.is_loose_file \
                    and upgrade.plan(app.detail_rec):
                app.act_upgrade(scr, app.detail_rec)
            elif kind == "char" and val == "i" and app.detail_item:
                app.act_install_item(scr, app.detail_item)
            elif kind == "char" and val == "d":
                if app.detail_item:
                    if app.detail_item.source == "apt":
                        app.act_download(scr, app.detail_item.name,
                                         app.detail_item.version)
                elif app.detail_rec and app.detail_rec.pkg_type == "deb" \
                        and not app.detail_rec.is_loose_file:
                    app.act_download(scr, app.detail_rec.name,
                                     app.detail_rec.candidate)
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
            # 同一个键在搜索视图里切来源、在包视图里循环披露层级：
            # 两个语义在各自视图里都用得上，且互不冲突
            if app.view == _SEARCH:
                app.cycle_source()
            else:
                app.cycle_level()
            continue
        if kind == "char" and val == "R":
            app.reload(scr)
            continue
        cur = app.current()
        if kind == "char" and val == "T" and (
                app.view in (_CLEAN, _LOOSE)
                or (cur and cur.kind == "pkg" and cur.data.is_loose_file)):
            app.trash = not app.trash
            app.msg = ("删除方式：移入回收站（可还原）" if app.trash
                       else "删除方式：真删")
            continue
        if kind == "char" and val == "t" and app.view in (_LIST, _UPGRADABLE,
                                                          _LOOSE):
            app.cycle_type()
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
        if app.view == _CLEAN and kind == "char" and val == "v":
            # 下钻：进入当前缓存目录的下一层，定位大缓存的具体来源
            it = app.current()
            if it and it.kind == "target" and it.data is not None \
                    and len(it.data.paths) == 1 and os.path.isdir(it.data.paths[0]):
                app.drill = it.data.paths[0]
                app.rebuild()
                app.msg = f"已进入 {app.drill}（v 继续下钻 · b 返回）"
            else:
                app.msg = "只有目录类条目可以下钻"
            continue
        if app.view == _CLEAN and kind == "char" and val in ("b",):
            if app.drill:
                parent = os.path.dirname(app.drill)
                app.drill = parent if "/.cache" in parent or "/.var/app" in parent \
                    or "/cache" in parent else None
                app.rebuild()
                app.msg = "已返回上一层" if app.drill else "已回到清理列表"
            continue
        if tui.is_enter(kind, val):
            it = app.current()
            if it and it.kind == "target":
                app.act_clean(scr, it.data)
            elif it:
                app.open_detail()
            continue
        if kind == "char" and val == "o":
            it = app.current()
            if it and it.kind == "pkg" and launch.plan(it.data):
                app.act_open(it.data)
            continue
        if kind == "char" and val == "r":
            it = app.current()
            if it and it.kind == "pkg":
                if it.data.is_loose_file:
                    app.act_delete_loose(scr, it.data)
                elif classify.is_removable(it.data):
                    app.act_remove(scr, it.data)
            continue
        if kind == "char" and val == "u":
            it = app.current()
            if it and it.kind == "pkg" and not it.data.is_loose_file \
                    and upgrade.plan(it.data):
                app.act_upgrade(scr, it.data)
            continue
        if kind == "char" and val == "i":
            it = app.current()
            if it and it.kind == "repo":
                app.act_install_item(scr, it.data)
            continue
        if kind == "char" and val == "d":
            it = app.current()
            if it and it.kind == "repo":
                if it.data.source == "apt":
                    app.act_download(scr, it.data.name, it.data.version)
            elif it and it.kind == "pkg" and it.data.pkg_type == "deb" \
                    and not it.data.is_loose_file:
                app.act_download(scr, it.data.name, it.data.candidate)
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
