"""pkgtool.apt.deps — 依赖串解析与独占依赖闭包的测试。

覆盖依赖字段的全部形态：逗号分隔、竖线可选项、版本约束括号、:arch 后缀；
以及闭包计算的三条规则：共用不记、manual 不记、环不死循环。
"""
import unittest

from pkgtool.apt.deps import DepGraph, dep_names
from pkgtool.apt.dpkg import StatusEntry


def entry(name, depends="", pre_depends="", size_kb=1000):
    return StatusEntry(name=name, version="1.0", status="install ok installed",
                       installed_size_kb=size_kb, depends=depends,
                       pre_depends=pre_depends)


class TestDepNames(unittest.TestCase):
    def test_simple_and_comma(self):
        self.assertEqual(dep_names("a, b", {"a", "b"}), {"a", "b"})

    def test_alternatives_pipe(self):
        # 竖线可选项：已安装的都算（保守计数）
        self.assertEqual(dep_names("a | b, c", {"a", "b", "c"}), {"a", "b", "c"})

    def test_version_constraint_and_arch(self):
        raw = "libc6 (>= 2.34):amd64, libfoo:any"
        self.assertEqual(dep_names(raw, {"libc6", "libfoo"}), {"libc6", "libfoo"})

    def test_virtual_package_dropped(self):
        # default-mta 这类虚拟包不在已安装集合里，自然丢弃
        self.assertEqual(dep_names("default-mta | mail-transport-agent", set()), set())


class TestDepGraph(unittest.TestCase):
    def test_exclusive_closure_chain(self):
        # app → liba → libc：全是独占，闭包含全链
        entries = {"app": entry("app", "liba"), "liba": entry("liba", "libc"),
                   "libc": entry("libc")}
        g = DepGraph(entries)
        self.assertEqual(g.exclusive("app"), {"liba", "libc"})
        own, dep, total = g.total_mb("app")
        self.assertEqual((own, dep, total), (1.0, 2.0, 3.0))

    def test_shared_dependency_excluded(self):
        # libfoo 被两个包依赖：不算任何一方的独占
        entries = {"app1": entry("app1", "libfoo"), "app2": entry("app2", "libfoo"),
                   "libfoo": entry("libfoo")}
        g = DepGraph(entries)
        self.assertEqual(g.exclusive("app1"), set())
        self.assertTrue(g.is_shared("libfoo"))

    def test_manual_mark_excluded(self):
        # 用户手动装的依赖 apt 不会自动清理，不该计入"能释放多少"
        entries = {"app": entry("app", "libx"), "libx": entry("libx")}
        g = DepGraph(entries, manual={"libx"})
        self.assertEqual(g.exclusive("app"), set())

    def test_cycle_does_not_hang(self):
        entries = {"a": entry("a", "b"), "b": entry("b", "a")}
        g = DepGraph(entries)
        self.assertEqual(g.exclusive("a"), {"b"})

    def test_self_dependency_ignored(self):
        entries = {"a": entry("a", "a, b"), "b": entry("b")}
        g = DepGraph(entries)
        self.assertEqual(g.exclusive("a"), {"b"})


if __name__ == "__main__":
    unittest.main()
