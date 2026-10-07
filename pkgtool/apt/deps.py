"""pkgtool.apt.deps — 已安装包的依赖图：独占依赖闭包与体积核算。

「独占依赖」= 只被这一个包硬依赖（Depends / Pre-Depends）、且 apt 标记为
auto 的包，取传递闭包。回答的问题是"这个软件拖了多少只属于它自己的东西"。

它和 `apt-get autoremove` 的答案不是一回事，两者不要混用：
  · 独占依赖        这个包**用到**的、没人共用的东西      → 体积核算用
  · autoremove 结果 删掉这个包后**连带孤立**的全部自动包   → 卸载预览用
后者含反向依赖级联，数字通常大得多：node-css-loader 的独占依赖是 0，
但删它会让 npm（依赖它）连带 396 个自动包一起走。卸载预览走 remove.py
的 apt dry-run，不用这里的数字。

依赖串解析要处理：逗号分隔的多项、竖线分隔的可选项、版本约束括号、
:arch 后缀。虚拟包（如 default-mta）不在已安装集合里，自然被丢弃。
"""
import re

_DEP_NAME_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9.+\-]*)")
_KB_PER_MB = 1024.0


def dep_names(raw, known):
    """依赖字段 → 已安装包名集合。raw 可以是多个字段拼接的串。"""
    out = set()
    for group in (raw or "").split(","):
        for alt in group.split("|"):
            m = _DEP_NAME_RE.match(alt.strip())
            if not m:
                continue
            name = m.group(1)
            if name in known:
                out.add(name)
    return out


class DepGraph:
    """硬依赖图。构造一次，多次查询（exclusive 结果带缓存）。"""

    def __init__(self, entries, manual=frozenset()):
        self._size = {n: e.installed_size_kb for n, e in entries.items()}
        self._deps = {n: dep_names(f"{e.depends}, {e.pre_depends}", entries)
                      for n, e in entries.items()}
        self._manual = set(manual)
        refcount = {}
        for deps in self._deps.values():
            for d in deps:
                refcount[d] = refcount.get(d, 0) + 1
        self._refcount = refcount
        self._cache = {}

    def __len__(self):
        return len(self._deps)

    def size_mb(self, name):
        """包自身已安装体积（来自 status 的 Installed-Size，不遍历文件树）。"""
        return round(self._size.get(name, 0) / _KB_PER_MB, 1)

    def is_shared(self, name):
        """被两个以上的包依赖 = 共用，删掉任何一个都不会释放它。"""
        return self._refcount.get(name, 0) > 1

    def _children(self, name):
        """name 的依赖中，只被 name 一个包依赖、且 apt 未标记为 manual 的那些。
        manual 标记的包 apt 不会自动清理，算进来会虚高"能释放多少"。"""
        out = []
        for d in self._deps.get(name, ()):
            if d == name or d in self._manual:
                continue
            if self._refcount.get(d, 0) == 1:
                out.append(d)
        return out

    def exclusive(self, name):
        """独占依赖的传递闭包（带环保护：A↔B 互相独占时不会死循环）。"""
        if name in self._cache:
            return self._cache[name]
        seen = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            for d in self._children(cur):
                if d == name or d in seen:
                    continue
                seen.add(d)
                stack.append(d)
        self._cache[name] = seen
        return seen

    def total_mb(self, name):
        """→ (自身体积, 独占依赖体积, 合计)。"""
        deps = self.exclusive(name)
        own = self.size_mb(name)
        extra = round(sum(self._size.get(d, 0) for d in deps) / _KB_PER_MB, 1)
        return own, extra, round(own + extra, 1)
