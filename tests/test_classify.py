"""pkgtool.classify — 卸载风险判定的测试。

用手工构造的 PackageRecord 覆盖四层证据链和各格式的判定规则；
apt 索引用真实的 RepoIndex 装 RepoEntry（classify 用 isinstance 校验，
不接受鸭子类型的假索引）。判定原则：判不准就保守，只有 APP 能删。
"""
import unittest

from pkgtool import classify
from pkgtool.apt.lists import RepoEntry, RepoIndex
from pkgtool.base import Channel, OriginKind, PackageRecord, PkgClass


def rec(**kw):
    defaults = dict(pkg_type="deb", name="app", version="1.0",
                    channel=Channel.APT, extra={})
    defaults.update(kw)
    return PackageRecord(**defaults)


def index_with(**meta):
    """构造只含 meta 信息的 apt 索引（classify 只用 meta()）。"""
    entries = {}
    for name, m in meta.items():
        entries[name] = [RepoEntry(
            version="1.0", repo="t", suite="", component="", arch="amd64",
            maintainer="", size_kb=0, filename="", date="",
            desc="", section=m.get("section", ""),
            priority=m.get("priority", ""))]
    return RepoIndex(entries)


class TestDebLayers(unittest.TestCase):
    def test_loose_file_is_file(self):
        r = rec(extra={"loose": "/tmp/x.deb"})
        self.assertEqual(classify.classify(r)[0], PkgClass.FILE)

    def test_critical_name_always_base(self):
        r = rec(name="bash", extra={"desktop_id": "bash"})   # 有 desktop 也不给删
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.BASE)

    def test_priority_required_base(self):
        r = rec(name="corepkg", extra={})
        idx = index_with(corepkg={"priority": "required"})
        self.assertEqual(classify.classify(r, idx)[0], PkgClass.BASE)

    def test_kernel_prefix_system(self):
        r = rec(name="linux-image-7.0.0", extra={})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.SYSTEM)

    def test_desktop_is_app(self):
        r = rec(extra={"desktop_id": "app"})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.APP)

    def test_opt_layout_is_app(self):
        r = rec(extra={"top_dirs": ["opt", "usr"]})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.APP)

    def test_local_deb_without_entry_is_library(self):
        # cuda-repo / cuda-keyring 一类：本地 .deb 但无应用入口，不能当"软件"
        r = rec(channel=Channel.DPKG_LOCAL, extra={})
        cls, reason = classify.classify(r, index_with())
        self.assertEqual(cls, PkgClass.LIBRARY)
        self.assertIn("无应用入口", reason)

    def test_local_deb_with_executables_is_app(self):
        r = rec(channel=Channel.APT_LOCAL, executables=["/usr/bin/app"], extra={})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.APP)

    def test_section_libs_library(self):
        r = rec(name="somelib1", extra={})
        idx = index_with(somelib1={"section": "libs"})
        self.assertEqual(classify.classify(r, idx)[0], PkgClass.LIBRARY)

    def test_no_executables_library(self):
        r = rec(name="mydata", extra={})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.LIBRARY)

    def test_manual_mark_with_exes_app(self):
        r = rec(name="mytool", executables=["/usr/bin/mytool"],
                extra={"apt_mark": "manual"})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.APP)

    def test_unknown_conservative_system(self):
        # 有可执行文件但既非 desktop/本地 .deb，又不是 manual 标记 → 保守 SYSTEM
        r = rec(name="weirdthing", executables=["/usr/bin/weirdthing"],
                extra={"apt_mark": "auto"})
        self.assertEqual(classify.classify(r, index_with())[0], PkgClass.SYSTEM)


class TestOtherFormats(unittest.TestCase):
    def test_snap_base_vs_app(self):
        self.assertEqual(classify.classify(rec(pkg_type="snap", name="bare"))[0],
                         PkgClass.BASE)
        self.assertEqual(classify.classify(rec(pkg_type="snap", name="firefox"))[0],
                         PkgClass.APP)

    def test_flatpak_runtime_vs_app(self):
        self.assertEqual(classify.classify(
            rec(pkg_type="flatpak-runtime", name="org.freedesktop.Platform"))[0],
            PkgClass.LIBRARY)
        self.assertEqual(classify.classify(
            rec(pkg_type="flatpak", name="com.brave.Browser"))[0],
            PkgClass.APP)

    def test_appimage_is_app(self):
        self.assertEqual(classify.classify(rec(pkg_type="appimage"))[0],
                         PkgClass.APP)


class TestFilters(unittest.TestCase):
    def test_is_removable_only_app(self):
        self.assertTrue(classify.is_removable(rec(pkg_class=PkgClass.APP)))
        self.assertFalse(classify.is_removable(rec(pkg_class=PkgClass.LIBRARY)))

    def test_visibility_levels(self):
        # 分级披露：0=仅软件，1=+库/数据/散落文件，2=+系统/基础
        self.assertEqual(classify.visibility_level(rec(pkg_class=PkgClass.APP)), 0)
        self.assertEqual(classify.visibility_level(rec(pkg_class=PkgClass.FILE)), 1)
        self.assertEqual(classify.visibility_level(rec(pkg_class=PkgClass.LIBRARY)), 1)
        self.assertEqual(classify.visibility_level(rec(pkg_class=PkgClass.SYSTEM)), 2)
        self.assertEqual(classify.visibility_level(rec(pkg_class=PkgClass.BASE)), 2)
        self.assertEqual(classify.visibility_level(rec(pkg_class=None)), 2)

    def test_user_installed(self):
        self.assertTrue(classify.is_user_installed(
            rec(origin_kind=OriginKind.LOCAL)))
        self.assertTrue(classify.is_user_installed(
            rec(channel=Channel.DPKG_LOCAL)))
        self.assertTrue(classify.is_user_installed(rec(pkg_type="appimage")))
        self.assertFalse(classify.is_user_installed(
            rec(pkg_type="flatpak-runtime", name="org.x.Platform")))
        self.assertFalse(classify.is_user_installed(rec(extra={"loose": "/x"})))


if __name__ == "__main__":
    unittest.main()
