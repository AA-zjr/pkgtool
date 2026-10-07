"""pkgtool.tui — curses 终端界面底座（标准库，无第三方依赖）。

两层：
  Screen    单屏原语：按显示宽度安全写入、颜色、按键（含中文宽字符）、
            底部输入行、确认/暂停，以及 run_external —— 让出终端跑子进程
  ListView  带光标与滚动的列表，行内容由调用方的 render 回调给出

上层（clean 的 browse、app.py 的主界面）都建在这两个类上，不直接碰 curses，
免得每处都重复处理宽字符越界、右下角写入抛异常、以及 endwin 的时序。
"""
import curses
import locale
import sys
from dataclasses import dataclass

from .report import truncate, width

_STATE_PENDING, _STATE_MARKED, _STATE_DONE, _STATE_FAIL = range(4)
_STATE_MARK = {_STATE_PENDING: " ", _STATE_MARKED: "*", _STATE_DONE: "✓",
               _STATE_FAIL: "✗"}

# 按键常量
ENTER = (curses.KEY_ENTER, 10, 13)
ESC = 27

# ESC 之后等多久判定它是单独的 Esc 还是转义序列的开头（毫秒）
_ESC_WAIT_MS = 40

# 自己维护的转义序列 → curses 键码映射。不查 terminfo，因为实测组装不可靠。
_ESC_MAP = {
    (91, 65): curses.KEY_UP,    (79, 65): curses.KEY_UP,
    (91, 66): curses.KEY_DOWN,  (79, 66): curses.KEY_DOWN,
    (91, 67): curses.KEY_RIGHT, (79, 67): curses.KEY_RIGHT,
    (91, 68): curses.KEY_LEFT,  (79, 68): curses.KEY_LEFT,
    (91, 72): curses.KEY_HOME,  (91, 49, 126): curses.KEY_HOME,
    (91, 70): curses.KEY_END,   (91, 52, 126): curses.KEY_END,
    (91, 71): curses.KEY_END,   (91, 53, 126): curses.KEY_PPAGE,
    (91, 54, 126): curses.KEY_NPAGE, (91, 51, 126): curses.KEY_DC,
    (91, 90): curses.KEY_BTAB,
}


@dataclass
class Row:
    label: str
    detail: str = ""
    note: str = ""
    size_mb: float = 0.0


def is_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


# get_wch 对普通字符返回 str、对功能键返回 int，而 getch 一律返回 int。
# 下面几个判定必须同时吃这两种形态，否则换输入法/换终端就会静默失灵
# （回归测试抓到过：hjkl 因为跟 ord("k") 比而整体失效）。
def is_enter(kind, val):
    return val in ENTER or (kind == "char" and val in ("\n", "\r"))


def is_quit(kind, val):
    return val == ESC or (kind == "char" and val in ("q", "Q"))


def is_backspace(kind, val):
    return (val in (curses.KEY_BACKSPACE, 127, 8)
            or (kind == "char" and val in ("\x7f", "\b")))


def size_text(mb):
    if mb <= 0:
        return "      —"
    return f"{mb / 1024:6.2f} GB" if mb >= 1024 else f"{mb:6.1f} MB"


def _init_colors():
    out = {}
    if not curses.has_colors():
        return out
    curses.start_color()
    curses.use_default_colors()
    pairs = (("title", curses.A_BOLD, curses.COLOR_CYAN),
             ("dim", 0, curses.COLOR_BLUE),
             ("sel", curses.A_BOLD, curses.COLOR_YELLOW),
             ("ok", 0, curses.COLOR_GREEN),
             ("bad", curses.A_BOLD, curses.COLOR_RED),
             ("warn", curses.A_BOLD, curses.COLOR_MAGENTA),
             ("norm", 0, -1))
    for i, (name, attr, fg) in enumerate(pairs, start=1):
        try:
            curses.init_pair(i, fg, -1)
            out[name] = attr | curses.color_pair(i)
        except curses.error:
            out[name] = attr
    return out


class Screen:
    """一块 curses 屏幕。所有写入都走这里，自动按显示宽度裁剪。"""

    def __init__(self, std):
        self.std = std
        std.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass                       # 某些终端不支持隐藏光标
        self.colors = _init_colors()

    @property
    def h(self):
        return self.std.getmaxyx()[0]

    @property
    def w(self):
        return self.std.getmaxyx()[1]

    def attr(self, name):
        return self.colors.get(name, 0)

    def clear(self):
        self.std.erase()

    def refresh(self):
        self.std.refresh()

    def text(self, y, x, s, color=None):
        """写一行；超出右边界按显示宽度裁掉（curses 写右下角那格会抛异常）。"""
        h, w = self.std.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = str(s)
        if width(s) > w - x:
            s = truncate(s, max(0, w - x - 1))
        try:
            self.std.addstr(y, x, s, self.colors.get(color, 0) if color else 0)
        except curses.error:
            pass

    def blank(self, y, color=None):
        self.text(y, 0, " " * (self.w - 1), color)

    def hline(self, y, color="dim"):
        self.text(y, 0, "─" * min(self.w - 1, 200), color)

    def read_key(self):
        """→ (kind, value)，kind 为 'char'（可打印字符，含中文）或 'key'（功能键）。

        两件事都不能依赖 ncurses：
          · get_wch 在部分构建上不遵守 keypad(True)，会把序列拆成字符返回；
          · getch 的转义序列组装依赖 terminfo 与 ESCDELAY，实测在
            TERM=xterm-256color 下方向键仍被拆成 27 / '[' / 'B' 三个字节。
        所以这里自己读字节、自己拼 UTF-8 与转义序列。"""
        k = self.std.getch()
        if k < 0:
            return ("key", -1)
        if k > 255:
            return ("key", k)              # ncurses 已经组装好的功能键
        if k == 27:
            return ("key", self._read_escape())
        if k < 128:
            return ("char", chr(k))
        need = {0xC0: 2, 0xD0: 2, 0xE0: 3, 0xF0: 4}.get(k & 0xF0, 1)
        raw = bytes([k])
        for _ in range(need - 1):
            b = self.std.getch()
            if b < 0 or b > 255:
                break
            raw += bytes([b])
        try:
            return ("char", raw.decode("utf-8"))
        except UnicodeDecodeError:
            return ("key", -1)

    def _read_escape(self):
        """ESC 之后短等一下：跟了字节就是转义序列，没跟就是用户真按了 Esc。"""
        self.std.timeout(_ESC_WAIT_MS)
        seq = []
        try:
            while len(seq) < 8:
                b = self.std.getch()
                if b < 0:
                    break
                seq.append(b)
                if len(seq) >= 2 and 0x40 <= b <= 0x7E:
                    break                       # 终止字节，序列结束
        finally:
            self.std.timeout(-1)                # 恢复阻塞读
        if not seq:
            return ESC
        return _ESC_MAP.get(tuple(seq), -2)     # -2 = 认不出的序列，忽略

    # ---- 让出终端：子进程要 sudo 密码提示或大量输出时，必须独占真实终端 ----

    def run_external(self, fn):
        """endwin → 执行 fn → 丢弃期间残留按键 → 恢复画面。"""
        curses.endwin()
        try:
            return fn()
        finally:
            curses.flushinp()
            self.std.refresh()

    def confirm(self, prompt, default=False):
        suffix = " [Y/n] " if default else " [y/N] "

        def go():
            try:
                ans = input(prompt + suffix).strip().lower()
            except EOFError:
                return default
            return default if not ans else ans in ("y", "yes")
        return self.run_external(go)

    def ask(self, prompt, default=""):
        """在真实终端上读一行（能用 readline 的历史/补全）。取消返回 None。"""
        def go():
            try:
                return input(prompt)
            except EOFError:
                return None
        return self.run_external(go)

    def pause(self, text="按 Enter 返回界面…"):
        try:
            self.run_external(lambda: input(text))
        except (KeyboardInterrupt, OSError):
            pass

    def prompt(self, label, initial="", live=None):
        """在底部一行内编辑输入（界面其余部分保持可见）。
        Enter 确认 → 返回字符串；Esc 取消 → 返回 None。
        live(text) 在每次按键后回调，用于即时过滤。"""
        buf = list(initial)
        while True:
            if live:
                live("".join(buf))
            self.text(self.h - 1, 0, " " * (self.w - 1))
            self.text(self.h - 1, 0, truncate(label + "".join(buf) + "▌", self.w - 1),
                      "title")
            self.refresh()
            kind, val = self.read_key()
            if is_enter(kind, val):
                return "".join(buf)
            if val == ESC:
                return None                          # Esc 一律取消
            if kind == "char" and val in ("q", "Q") and not buf:
                return None                          # 空输入时 q 也算取消
            if is_backspace(kind, val):
                if buf:
                    buf.pop()
            elif kind == "char" and val >= " ":
                buf.append(val)


class ListView:
    """光标 + 滚动 + 渲染。items 是不透明对象，行内容由 render 决定。

    render(item, selected) → [(文本, 颜色名), ...]，长度 1 或 2（对应 per_item）。
    """

    def __init__(self, items, render, per_item=2):
        self.items = list(items)
        self.render = render
        self.per_item = per_item
        self.cursor = 0
        self.top = 0
        self.visible = 1

    def set_items(self, items, keep_cursor=False):
        old = self.items[self.cursor] if keep_cursor and self.items else None
        self.items = list(items)
        self.cursor = 0
        if old is not None:
            for i, it in enumerate(self.items):
                if it is old:
                    self.cursor = i
                    break
        self.top = min(self.top, max(0, len(self.items) - 1))

    @property
    def current(self):
        if not self.items:
            return None
        return self.items[self.cursor]

    def move(self, delta):
        if self.items:
            self.cursor = max(0, min(len(self.items) - 1, self.cursor + delta))

    def goto(self, idx):
        if self.items:
            self.cursor = max(0, min(len(self.items) - 1, idx))

    def page(self, direction):
        self.move(direction * max(1, self.visible - 1))

    def handle_key(self, kind, val):
        """通用导航键：方向键/PgUp/PgDn 走 'key'，hjkl/g/G 走 'char'。
        返回 True 表示已消费。"""
        if kind == "key":
            if val == curses.KEY_UP:
                self.move(-1)
            elif val == curses.KEY_DOWN:
                self.move(1)
            elif val == curses.KEY_HOME:
                self.goto(0)
            elif val == curses.KEY_END:
                self.goto(len(self.items) - 1)
            elif val == curses.KEY_NPAGE:
                self.page(1)
            elif val == curses.KEY_PPAGE:
                self.page(-1)
            else:
                return False
            return True
        if val == "k":
            self.move(-1)
        elif val == "j":
            self.move(1)
        elif val == "g":
            self.goto(0)
        elif val == "G":
            self.goto(len(self.items) - 1)
        else:
            return False
        return True

    def draw(self, scr, y0, height):
        """在 [y0, y0+height) 区域画出可见项，返回可见条数。"""
        per = self.per_item
        self.visible = max(1, height // per)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + self.visible:
            self.top = self.cursor - self.visible + 1
        y = y0
        for idx in range(self.top, min(self.top + self.visible, len(self.items))):
            lines = self.render(self.items[idx], idx == self.cursor)
            for j, (txt, color) in enumerate(lines[:per]):
                scr.text(y + j, 0 if j == 0 else 3, txt, color)
            y += per
        return self.visible


# ---------- clean 用的逐项删除列表 ----------


@dataclass
class _Item:
    row: Row
    state: int = _STATE_PENDING
    message: str = ""


def browse(title, rows, action, hint="", on_quit=None):
    """交互式删除列表。action(index) -> (ok: bool, message: str)。
    返回 (尝试次数, 成功次数, 释放 MB)；非 tty 或 curses 不可用返回 None。"""
    if not rows or not is_tty():
        return None
    try:
        locale.setlocale(locale.LC_ALL, "")   # curses 正确处理中文/宽字符的前提
        return curses.wrapper(_browse_main, title, [_Item(r) for r in rows],
                              action, hint, on_quit)
    except curses.error:
        return None                           # 终端太小或不支持，交给调用方降级


def _browse_main(std, title, items, action, hint, on_quit):
    scr = Screen(std)
    tried = done = 0
    freed = 0.0

    def render(it, selected):
        color = "sel" if selected else "norm"
        if it.state == _STATE_DONE:
            color = "ok"
        elif it.state == _STATE_FAIL:
            color = "bad"
        mark = f"{_STATE_MARK[it.state]}{'>' if selected else ' '}"
        head = f"{mark} {size_text(it.row.size_mb)}  {it.row.label}"
        sub = it.message or " · ".join(x for x in (it.row.detail, it.row.note) if x)
        return [(head, color), (sub, "dim")]

    view = ListView(items, render, per_item=2)
    while True:
        h, w = scr.h, scr.w
        if h < 8 or w < 40:
            scr.clear()
            scr.text(0, 0, "终端窗口太小，请放大后重试（至少 8 行 40 列）")
            scr.refresh()
            kind, val = scr.read_key()
            if is_quit(kind, val):
                break
            continue

        scr.clear()
        total = sum(i.row.size_mb for i in items)
        scr.text(0, 0, f" {title} · {len(items)} 项 · 可回收 {size_text(total).strip()}",
                 "title")
        scr.hline(1)
        per = 2 if h >= 24 else 1
        view.per_item = per
        view.draw(scr, 2, h - 4)

        scr.hline(h - 2)
        marked = sum(1 for i in items if i.state == _STATE_MARKED)
        status = (f" 已释放 {size_text(freed).strip()} · 成功 {done}/{tried}"
                  + (f" · 已标记 {marked}" if marked else "")
                  + "  |  " + (hint or "Enter 删除 · 空格 标记 · d 删标记 · q 退出"))
        scr.text(h - 1, 0, status, "title")
        scr.refresh()

        kind, val = scr.read_key()
        if is_quit(kind, val):
            break
        if view.handle_key(kind, val):
            continue
        if kind == "char" and val == " ":
            it = view.current
            if it and it.state in (_STATE_PENDING, _STATE_MARKED):
                it.state = (_STATE_MARKED if it.state == _STATE_PENDING
                            else _STATE_PENDING)
            view.move(1)
        elif kind == "char" and val == "a":
            for it in items:
                if it.state == _STATE_PENDING:
                    it.state = _STATE_MARKED
        elif kind == "char" and val == "A":
            for it in items:
                if it.state == _STATE_MARKED:
                    it.state = _STATE_PENDING
        elif is_enter(kind, val):
            idx = view.cursor
            it = items[idx]
            tried += 1
            ok, mb = _run(scr, it, idx, action)
            done += 1 if ok else 0
            freed += mb
            view.move(1)
        elif kind == "char" and val == "d":
            for idx, it in enumerate(items):
                if it.state != _STATE_MARKED:
                    continue
                tried += 1
                ok, mb = _run(scr, it, idx, action)
                done += 1 if ok else 0
                freed += mb

    if on_quit:
        on_quit(tried, done, freed)
    return tried, done, freed


def _run(scr, item, idx, action):
    """执行一项。期间让出终端：特权目标会触发 sudo 密码提示，那个提示和
    curses 画面会互相覆盖；结果同时打印到终端，给不可逆操作留审计记录。"""
    def go():
        print(f"→ {item.row.label} …", flush=True)
        try:
            ok, msg = action(idx)
        except Exception as e:                # noqa: BLE001 单项失败不该炸掉界面
            ok, msg = False, f"{type(e).__name__}: {e}"
        print(f"  {'✓' if ok else '✗'} {msg}", flush=True)
        return ok, msg

    ok, _msg = scr.run_external(go)
    item.state = _STATE_DONE if ok else _STATE_FAIL
    item.message = _msg or ("已删除" if ok else "失败")
    return ok, item.row.size_mb if ok else 0.0
