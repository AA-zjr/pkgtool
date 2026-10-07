"""APT 仓库索引：解析本地 /var/lib/apt/lists，支持搜索、版本比较、升级检测。

无需 root：lists 文件全局可读；下载用 apt-get download（非 root 可用）。
"""
import glob
import gzip
import os
import re
import time
from email.utils import parsedate_to_datetime

_LISTS = "/var/lib/apt/lists"
_cache = {"t": 0.0, "idx": None}
_TTL = 300  # 5 分钟（apt update 后自动重新解析）


def _read(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


# ---------- Debian 版本比较（无 python-debian，自实现） ----------

def _segs(s):
    out, i = [], 0
    while i < len(s):
        if s[i].isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            out.append(("d", int(s[i:j])))
            i = j
        else:
            j = i
            while j < len(s) and not s[j].isdigit():
                j += 1
            out.append(("s", s[i:j]))
            i = j
    return out


def _cmp_segstr(a, b):
    """非数字段逐字符：~ 排最前（连空串都小于），其余按 ASCII+1，结束(0) < 任何非~字符。"""
    for k in range(max(len(a), len(b))):
        ca = a[k] if k < len(a) else None
        cb = b[k] if k < len(b) else None
        va = -1 if ca == "~" else (0 if ca is None else ord(ca) + 1)
        vb = -1 if cb == "~" else (0 if cb is None else ord(cb) + 1)
        if va != vb:
            return -1 if va < vb else 1
    return 0


def _cmp_str(a, b):
    """Debian verrev：数字段按数值比较，非数字段按字符规则，混合时按首字符。"""
    sa, sb = _segs(a), _segs(b)
    i = j = 0
    while i < len(sa) or j < len(sb):
        ta, va = sa[i] if i < len(sa) else ("e", None)
        tb, vb = sb[j] if j < len(sb) else ("e", None)
        if ta == "d" and tb == "d":
            c = (va > vb) - (va < vb)
        elif ta == "s" and tb == "s":
            c = _cmp_segstr(va, vb)
        else:  # 数字段 vs 字符串/结束：dpkg 字符分支只比首字符，此处必不相等
            def first(t, v):
                if t == "e":
                    return 0
                if t == "d":
                    return ord(str(v)[0]) + 1
                return -1 if v[0] == "~" else ord(v[0]) + 1
            fa, fb = first(ta, va), first(tb, vb)
            c = (fa > fb) - (fa < fb)
        i += 1
        j += 1
        if c:
            return c
    return 0


def ver_cmp(a, b):
    """Debian 版本比较 → -1/0/1。支持 epoch:upstream-revision。"""
    def parts(v):
        v = v.strip()
        epoch = 0
        if ":" in v:
            e, v = v.split(":", 1)
            epoch = int(e)
        up, _, rev = v.rpartition("-")
        if not up:
            up, rev = v, ""
        return epoch, up, rev
    ea, ua, ra = parts(a)
    eb, ub, rb = parts(b)
    if ea != eb:
        return -1 if ea < eb else 1
    c = _cmp_str(ua, ub)
    return c if c else _cmp_str(ra, rb)


def ver_gt(a, b):
    return ver_cmp(a, b) > 0


# ---------- Release 日期（仓库快照时间） ----------

def _release_date_for(fname):
    """按文件名前缀匹配对应的 InRelease/Release，取 Date 字段。"""
    base = fname[:-3] if fname.endswith(".gz") else fname
    core = base.split("_binary-")[0]
    parts = core.split("_")
    for i in range(len(parts), 1, -1):
        pref = "_".join(parts[:i])
        for cand in (pref + "_InRelease", pref + "_Release"):
            p = os.path.join(_LISTS, cand)
            if os.path.isfile(p):
                m = re.search(r"^Date: (.+)$", _read(p), re.M)
                if m:
                    try:
                        return parsedate_to_datetime(m.group(1).strip()).strftime("%Y-%m-%d")
                    except Exception:  # noqa: BLE001
                        return m.group(1).strip()
    return None


_COMP = {"main", "universe", "multiverse", "restricted", "contrib", "non-free",
         "non-free-firmware", "upstream"}


def _suite_comp(fname):
    base = fname[:-3] if fname.endswith(".gz") else fname
    core = base.split("_binary-")[0]
    toks = core.split("_")
    comp = toks[-1] if toks[-1] in _COMP else ""
    suite = toks[-2] if len(toks) >= 2 and (comp or toks[-2] not in ("dists", "deb")) else ""
    return suite, comp


# ---------- 索引 ----------

def load_index():
    """→ {name: {versions:[{version,suite,component,maintainer,size_kb,filename,date}], latest:str}}
    只收 amd64/all 架构；缓存 5 分钟。"""
    now = time.time()
    if _cache["idx"] is not None and now - _cache["t"] < _TTL:
        return _cache["idx"]
    idx = {}
    files = sorted(set(glob.glob(os.path.join(_LISTS, "*_Packages"))
                       + glob.glob(os.path.join(_LISTS, "*_Packages.gz"))))
    for f in files:
        fname = os.path.basename(f)
        if re.search(r"_(arm64|armhf|i386|i386|ppc64el|s390x)_binary", fname):
            continue
        suite, comp = _suite_comp(fname)
        rdate = _release_date_for(fname)
        try:
            text = _read(f)
        except OSError:
            continue
        for block in re.split(r"\n\n+", text):
            pkg = ver = arch = maint = size = fn = desc = ""
            for line in block.splitlines():
                if not line or line[0] in " \t":
                    continue
                k, _, v = line.partition(": ")
                v = v.strip()
                if k == "Package" and not pkg:
                    pkg = v
                elif k == "Version" and not ver:
                    ver = v
                elif k == "Architecture" and not arch:
                    arch = v
                elif k == "Maintainer" and not maint:
                    maint = v
                elif k == "Size" and not size:
                    size = v
                elif k == "Filename" and not fn:
                    fn = v
                elif k == "Description" and not desc:
                    desc = v[:160]
            if not pkg or not ver or arch not in ("amd64", "all"):
                continue
            e = idx.setdefault(pkg, {"versions": []})
            e["versions"].append({
                "version": ver, "suite": suite, "component": comp,
                "maintainer": maint or "?",
                "size_kb": int(size) // 1024 if size.isdigit() else 0,
                "filename": fn, "date": rdate,
                "desc": desc if not e["versions"] else "",
            })
    for e in idx.values():
        best = e["versions"][0]["version"]
        for v in e["versions"][1:]:
            if ver_gt(v["version"], best):
                best = v["version"]
        e["latest"] = best
    _cache.update(t=now, idx=idx)
    return idx


def search(q, limit=30):
    """按包名/描述搜索 → [{name, version, maintainer, size_kb, source, date, desc}]"""
    q = (q or "").lower().strip()
    if not q:
        return []
    out = []
    for name, e in load_index().items():
        top = next((v for v in e["versions"] if v["version"] == e["latest"]), e["versions"][-1])
        hay_name = name.lower()
        if q in hay_name or (top.get("desc") and q in top["desc"].lower()):
            out.append({
                "name": name, "version": e["latest"],
                "maintainer": top["maintainer"], "size_kb": top["size_kb"],
                "source": "/".join(x for x in (top["suite"], top["component"]) if x),
                "date": top["date"], "desc": top.get("desc", ""),
            })
        if len(out) >= limit * 3:  # 先多收一些再排序
            break
    out.sort(key=lambda r: (0 if q in r["name"].lower() else 1, r["name"]))
    return out[:limit]


def info(name):
    """单包详情：全部版本 + latest。"""
    e = load_index().get(name)
    if not e:
        return None
    return {"name": name, "latest": e["latest"], "versions": e["versions"]}
