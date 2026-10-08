"""pkgtool.apt.logs — dpkg.log / apt history 解析的测试。

日志文件用临时文件喂给 Config（frozen dataclass 用 replace 覆盖 glob），
不碰真实系统日志。重点：dpkg.log 的字段位置（第 3 字段是安装前版本，
新装时是 <none>，第 4 字段才是装上的版本）、apt history 括号内的
", automatic" 不能按逗号拆条、首次 install 优先、系统出生时间窗。
"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pkgtool.apt import logs
from pkgtool.config import CFG


class TestParseTs(unittest.TestCase):
    def test_ok_and_bad(self):
        self.assertEqual(logs.parse_ts("2026-01-02 03:04:05").year, 2026)
        self.assertIsNone(logs.parse_ts("not a date"))
        self.assertIsNone(logs.parse_ts(""))


class TestDpkgInstalls(unittest.TestCase):
    def test_first_install_wins_and_version_position(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "dpkg.log")
            log.write_text(
                "2026-01-01 10:00:00 install foo:amd64 <none> 1.0\n"
                "2026-01-02 10:00:00 install foo:amd64 1.0 1.1\n"   # 升级不算首装
                "2026-01-01 09:00:00 startup archives unpack\n"     # 非 install 行
                "2026-01-01 10:00:00 install bar:all <none> 2.0\n",
                encoding="utf-8")
            cfg = replace(CFG, dpkg_log_glob=str(log))
            first, earliest = logs.dpkg_installs(cfg)
            self.assertEqual(first["foo"], ("2026-01-01 10:00:00", "1.0"))
            self.assertEqual(first["bar"][1], "2.0")
            # earliest 只统计 install 事件（startup 等行不参与）
            self.assertEqual(earliest, "2026-01-01 10:00:00")

    def test_upgrade_line_keeps_old_first(self):
        # 升级行的第 3 字段是旧版本：首装时间仍取该行（若之前无记录）
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "dpkg.log")
            log.write_text("2026-02-01 08:00:00 install pkg:amd64 1.0 1.1\n",
                           encoding="utf-8")
            first, _ = logs.dpkg_installs(replace(CFG, dpkg_log_glob=str(log)))
            self.assertEqual(first["pkg"][1], "1.1")   # 第 4 字段才是装上的版本


class TestAptHistory(unittest.TestCase):
    def test_install_and_upgrade_entries(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "history.log")
            log.write_text(
                "Start-Date: 2026-03-01  12:00:00\n"
                "Commandline: apt-get install -y foo\n"
                "Install: foo:amd64 (1.0), libdep:amd64 (0.5, automatic)\n"
                "End-Date: 2026-03-01  12:00:10\n"
                "Start-Date: 2026-03-02  08:00:00\n"
                "Upgrade: foo:amd64 (1.0, 1.1)\n",
                encoding="utf-8")
            cfg = replace(CFG, apt_history_glob=str(log))
            hist = logs.apt_history(cfg)
            self.assertIn(("2026-03-01 12:00:00", "1.0"), hist["foo"])
            self.assertIn(("2026-03-01 12:00:00", "0.5"), hist["libdep"])
            self.assertIn(("2026-03-02 08:00:00", "1.0, 1.1"), hist["foo"])
            # ", automatic" 标记被剥离，但 Upgrade 的双版本保留
            self.assertNotIn("automatic", str(hist))


class TestBirthCutoff(unittest.TestCase):
    def test_margin_window(self):
        dt = logs.birth_cutoff("2026-01-01 10:00:00", replace(CFG, birth_margin_hours=24))
        self.assertEqual(dt.strftime("%Y-%m-%d %H:%M:%S"), "2026-01-02 10:00:00")

    def test_missing_log_returns_none(self):
        self.assertIsNone(logs.birth_cutoff("", CFG))


if __name__ == "__main__":
    unittest.main()
