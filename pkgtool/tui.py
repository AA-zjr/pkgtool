"""pkgtool.tui — 终端交互列表（标准库 curses，无第三方依赖）。

只负责"画列表 + 收键盘"，删什么、怎么删由调用方传进来的 action 回调决定，
所以 clean 和以后其他需要逐条确认的命令可以共用这一套。

按键：↑↓/jk 移动 · Enter 删当前项 · 空格 标记 · d 删全部标记 · a 全标 ·
      A 全不标 · q 退出
非 tty（管道、重定向、CI）或 curses 不可用时 browse() 返回 None，
由调用方降级到非交互流程。
"""
import curses
import locale
import sys
from dataclasses import dataclass

from .report import truncate, width

_STATE_PENDING, _STATE_MARKED, _STATE_DONE, _STATE_FAIL = range(4)
_STATE_MARK = {_STATE_PENDING: " ", _STATE_MARKED: "*", _STATE_DONE: "✓",
               _STATE_FAIL: "✗"}


@dataclass
class Row:
    label: str
    detail: str = ""
    note: str = ""
    size_mb: float = 0.0


@dataclass
class _Item:
    row: Row
    state: int = _STATE_PENDING
    message: str = ""


def is_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _size_text(mb):
    if mb <= 0:
        return "       —"
    return f"{mb / 1024:6.2f} GB" if mb >= 1024 else f"{mb:6.1f} MB"


def browse(title, rows, action, hint="", on_quit=None):
    """交互式列表。action(index) -> (ok: bool, message: str)。
    返回 (尝试次数, 成功次数, 释放 MB)；非 tty 或 curses 不可用返回 None。"""
    if not rows or not is_tty():
        return None
    try:
        locale.setlocale(locale.LC_ALL, "")     # curses 正确处理中文/宽字符的前提
        return curses.wrapper(_main, title, [_Item(r) for r in rows],
                              action, hint, on_quit)
    except curses.error:
        return None                             # 终端太小或不支持，交给调用方降级


def _main(std, title, items, action, hint, on_quit):
    curses.curs_set(0)
    std.keypad(True)
    colors = _init_colors()
    cursor, top = 0, 0
    tried = done = 0
    freed = 0.0

    while True:
        h, w = std.getmaxyx()
        if h < 8 or w < 40:
            std.erase()
            _safe(std, 0, 0, "终端窗口太小，请放大后重试（至少 8 行 40 列）")
            std.refresh()
            if std.getch() in (ord("q"), ord("Q"), 27):
                break
            continue

        body_h = h - 4                          # 标题 1 + 分隔 1 + 底部 2
        per_item = 2 if h >= 24 else 1          # 窗口矮时每项只占一行
        visible = max(1, body_h // per_item)
        if cursor < top:
            top = cursor
        elif cursor >= top + visible:
            top = cursor - visible + 1

        std.erase()
        total = sum(i.row.size_mb for i in items)
        _safe(std, 0, 0, truncate(f" {title} · {len(items)} 项 · "
                                  f"可回收 {_size_text(total).strip()}", w - 1),
              colors.get("title"))
        _safe(std, 1, 0, "─" * min(w - 1, 200), colors.get("dim"))

        y = 2
        for idx in range(top, min(top + visible, len(items))):
            it = items[idx]
            sel = idx == cursor
            attr = colors.get("sel" if sel else "norm", 0)
            if it.state == _STATE_DONE:
                attr = colors.get("ok", attr)
            elif it.state == _STATE_FAIL:
                attr = colors.get("bad", attr)
            prefix = f"{_STATE_MARK[it.state]}{'>' if sel else ' '}"
            line1 = (f"{prefix} {_size_text(it.row.size_mb)}  "
                     f"{truncate(it.row.label, max(8, w - 22))}")
            _safe(std, y, 0, line1.ljust(min(w - 1, width(line1))), attr)
            if per_item == 2:
                note = it.message or " · ".join(
                    x for x in (it.row.detail, it.row.note) if x)
                _safe(std, y + 1, 3, truncate(note, max(8, w - 5)),
                      colors.get("dim"))
            y += per_item

        _safe(std, h - 2, 0, "─" * min(w - 1, 200), colors.get("dim"))
        marked = sum(1 for i in items if i.state == _STATE_MARKED)
        status = (f" 已释放 {_size_text(freed).strip()} · 成功 {done}/{tried}"
                  + (f" · 已标记 {marked}" if marked else "")
                  + "  |  " + (hint or "Enter 删除 · 空格 标记 · d 删标记 · q 退出"))
        _safe(std, h - 1, 0, truncate(status, w - 1), colors.get("title"))
        std.refresh()

        key = std.getch()
        if key in (ord("q"), ord("Q"), 27):                     # q / Esc
            break
        elif key in (curses.KEY_UP, ord("k")):
            cursor = max(0, cursor - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            cursor = min(len(items) - 1, cursor + 1)
        elif key in (curses.KEY_HOME, ord("g")):
            cursor = 0
        elif key in (curses.KEY_END, ord("G")):
            cursor = len(items) - 1
        elif key in (curses.KEY_NPAGE,):
            cursor = min(len(items) - 1, cursor + visible)
        elif key in (curses.KEY_PPAGE,):
            cursor = max(0, cursor - visible)
        elif key == ord(" "):
            it = items[cursor]
            if it.state in (_STATE_PENDING, _STATE_MARKED):
                it.state = _STATE_MARKED if it.state == _STATE_PENDING else _STATE_PENDING
            cursor = min(len(items) - 1, cursor + 1)
        elif key == ord("a"):
            for it in items:
                if it.state == _STATE_PENDING:
                    it.state = _STATE_MARKED
        elif key == ord("A"):
            for it in items:
                if it.state == _STATE_MARKED:
                    it.state = _STATE_PENDING
        elif key in (curses.KEY_ENTER, 10, 13):
            tried += 1
            ok, mb = _run(items[cursor], cursor, action, std)
            done += 1 if ok else 0
            freed += mb
            cursor = min(len(items) - 1, cursor + 1)
        elif key == ord("d"):
            for idx, it in enumerate(items):
                if it.state != _STATE_MARKED:
                    continue
                tried += 1
                ok, mb = _run(it, idx, action, std)
                done += 1 if ok else 0
                freed += mb

    if on_quit:
        on_quit(tried, done, freed)
    return tried, done, freed


def _run(item, idx, action, std):
    """执行一项。
    期间必须先 endwin() 让出终端：特权目标会触发 sudo 密码提示，那个提示
    和 curses 画面会互相覆盖，用户在花屏里输密码是不可接受的。
    结果同时打印到终端，留下滚动可查的审计记录（删除是不可逆操作）。"""
    curses.endwin()
    print(f"→ {item.row.label} …", flush=True)
    try:
        ok, msg = action(idx)
    except Exception as e:                    # noqa: BLE001 单项失败不该炸掉整个界面
        ok, msg = False, f"{type(e).__name__}: {e}"
    print(f"  {'✓' if ok else '✗'} {msg}", flush=True)
    curses.flushinp()                         # 丢掉执行期间残留的按键
    std.refresh()                             # 恢复 curses 画面
    item.state = _STATE_DONE if ok else _STATE_FAIL
    item.message = msg or ("已删除" if ok else "失败")
    return ok, item.row.size_mb if ok else 0.0


def _safe(std, y, x, text, attr=0):
    """curses 在写到右下角那一格时会抛异常，统一在这里裁掉。"""
    h, w = std.getmaxyx()
    if y >= h or x >= w:
        return
    text = str(text)
    if width(text) > w - x:
        text = truncate(text, max(0, w - x - 1))
    try:
        std.addstr(y, x, text, attr)
    except curses.error:
        pass


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
             ("norm", 0, -1))
    for i, (name, attr, fg) in enumerate(pairs, start=1):
        try:
            curses.init_pair(i, fg, -1)
            out[name] = attr | curses.color_pair(i)
        except curses.error:
            out[name] = attr
    return out
