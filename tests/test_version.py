"""pkgtool.apt.version — Debian 版本比较的回归测试。

用 dpkg 官方语义的典型用例：epoch、~（预发布）、数字段与前导零、
revision 与 upstream 的分离。
"""
import unittest

from pkgtool.apt.version import newest, ver_cmp, ver_gt


class TestVerCmp(unittest.TestCase):
    def test_basic(self):
        self.assertLess(ver_cmp("1.0", "2.0"), 0)
        self.assertEqual(ver_cmp("1.0", "1.0"), 0)
        self.assertGreater(ver_cmp("1.0", "1"), 0)      # 空段按结束符 < 字符

    def test_epoch(self):
        self.assertGreater(ver_cmp("2:1.0", "1:9.9"), 0)
        self.assertGreater(ver_cmp("1:0.1", "0.99"), 0)  # 无 epoch = 0
        self.assertEqual(ver_cmp("0:1.0", "1.0"), 0)

    def test_tilde_is_pre_release(self):
        # ~ 排在一切之前：预发布 < 正式版
        self.assertLess(ver_cmp("1.0~rc1", "1.0"), 0)
        self.assertLess(ver_cmp("1.0~beta", "1.0~rc1"), 0)
        self.assertLess(ver_cmp("1.0~rc1", "1.0~rc1+really"), 0)

    def test_revision_separated_from_upstream(self):
        self.assertLess(ver_cmp("1.0-1", "1.0-2"), 0)
        self.assertLess(ver_cmp("1.0", "1.0-1"), 0)      # 有 revision > 无
        # 最后一个连字符才是 revision 分隔：上游版本里的连字符不参与
        self.assertGreater(ver_cmp("1.0-1-2", "1.0-1"), 0)

    def test_numeric_segments_compare_numerically(self):
        self.assertLess(ver_cmp("1.9", "1.10"), 0)       # 不是字符串比较
        self.assertEqual(ver_cmp("1.01", "1.1"), 0)      # 前导零无意义
        self.assertLess(ver_cmp("1.0.20240101", "1.0.20240201"), 0)

    def test_letters_vs_symbols(self):
        # dpkg order：字母排在所有非字母符号之前 → 1.0a < 1.0+dfsg；
        # 但字母 > 字符串结束 → 1.0a > 1.0
        self.assertLess(ver_cmp("1.0a", "1.0+dfsg"), 0)
        self.assertGreater(ver_cmp("1.0a", "1.0"), 0)


class TestVerGtAndNewest(unittest.TestCase):
    def test_ver_gt(self):
        self.assertTrue(ver_gt("2:1.0", "1.0"))
        self.assertFalse(ver_gt("1.0~rc1", "1.0"))
        self.assertFalse(ver_gt("1.0", "1.0"))

    def test_newest(self):
        self.assertEqual(newest(["1.0", "2.0~rc1", "1.9", "2.0~rc1+really"]),
                         "2.0~rc1+really")
        self.assertEqual(newest(["1.0-1ubuntu0.1", "1.0-1"]), "1.0-1ubuntu0.1")
        self.assertEqual(newest([]), "")


if __name__ == "__main__":
    unittest.main()
