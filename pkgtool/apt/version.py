"""pkgtool.apt.version — Debian 版本号比较（dpkg verrev 算法，纯标准库实现）。

版本格式：[epoch:]upstream_version[-debian_revision]
比较规则（dpkg/version.c）：
  · epoch 按整数比，缺省为 0
  · upstream 与 revision 各自交替比较「非数字段」和「数字段」：
      非数字段逐字符按 order() 排序，数字段整体按数值（前导零无意义）
  · order(): '~' < 字符串结束 < 数字 < 字母 < 其他符号
"""


def _order(ch):
    """dpkg 的字符序。ch=None 表示字符串已结束。"""
    if ch == "~":
        return -1
    if ch is None:
        return 0
    if ch.isdigit():
        return 0
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _nondigit(s, i):
    j = i
    while j < len(s) and not s[j].isdigit():
        j += 1
    return s[i:j], j


def _digit(s, i):
    j = i
    while j < len(s) and s[j].isdigit():
        j += 1
    return s[i:j], j


def _cmp_part(a, b):
    """交替比较非数字段与数字段，直到两边都走完。"""
    i = j = 0
    while i < len(a) or j < len(b):
        sa, i = _nondigit(a, i)
        sb, j = _nondigit(b, j)
        for k in range(max(len(sa), len(sb))):
            ca = sa[k] if k < len(sa) else None
            cb = sb[k] if k < len(sb) else None
            oa, ob = _order(ca), _order(cb)
            if oa != ob:
                return -1 if oa < ob else 1
        da, i = _digit(a, i)
        db, j = _digit(b, j)
        na = int(da) if da else 0
        nb = int(db) if db else 0
        if na != nb:
            return -1 if na < nb else 1
    return 0


def _split(v):
    """→ (epoch, upstream, revision)。"""
    v = (v or "").strip()
    epoch = 0
    if ":" in v:
        head, v = v.split(":", 1)
        epoch = int(head) if head.isdigit() else 0
    up, sep, rev = v.rpartition("-")   # 最后一个连字符之后才是 debian revision
    if not sep:
        up, rev = v, ""
    return epoch, up, rev


def ver_cmp(a, b):
    """Debian 版本比较 → -1 / 0 / 1。"""
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


def ver_gt(a, b):
    return ver_cmp(a, b) > 0


def newest(versions):
    """一组版本字符串里最高的那个；空序列返回 ""。"""
    best = ""
    for v in versions:
        if not best or ver_gt(v, best):
            best = v
    return best
