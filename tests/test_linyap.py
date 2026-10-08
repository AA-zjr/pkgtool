"""pkgtool.backends.linyap — 如意玲珑后端的测试。

fixture 直接取自本机 ll-cli list --json 的真实输出（VSCode 应用 +
deepin base 层），覆盖：解析成记录、变体/来源/体积/首装时间、
states.json 降级、分类（应用 vs 运行时）、启动/升级/卸载命令的拼装。
"""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pkgtool.base import PkgClass
from pkgtool.backends.linyap import LinyapBackend
from pkgtool.classify import classify, is_user_installed
from pkgtool.config import CFG
from pkgtool.launch import plan as launch_plan
from pkgtool.remove import _linyap_steps
from pkgtool.upgrade import plan as upgrade_plan

REAL_LIST_JSON = json.dumps([
    {"arch": ["x86_64"], "base": "org.deepin.base/23.1.0", "channel": "main",
     "command": ["vscode"], "description": "VSCode代码编辑器\n",
     "id": "com.visualstudio.code", "install_time": 1791450508,
     "kind": "app", "module": "binary", "name": "Visual Studio Code",
     "schema_version": "1.0", "size": 447261440, "version": "1.105.1.176"},
    {"arch": ["x86_64"], "base": "org.deepin.foundation/20.0.2",
     "channel": "main", "description": "deepin base environment.\n",
     "id": "org.deepin.base", "install_time": 1791450541,
     "kind": "runtime", "module": "binary", "name": "deepin-foundation",
     "permissions": {}, "runtime": "latest", "schema_version": "1.0",
     "size": 413747564, "version": "23.1.0.3"},
])


def run_backend(cfg):
    return LinyapBackend(cfg).collect()


class TestCollect(unittest.TestCase):
    def test_real_fixture_to_records(self):
        cfg = replace(CFG, linyap_repo_dir="/nonexistent")
        with mock.patch("pkgtool.backends.linyap.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=REAL_LIST_JSON)
            recs = run_backend(cfg)
        self.assertEqual(len(recs), 2)
        app = recs[0]
        self.assertEqual(app.pkg_type, "linyap")
        self.assertEqual(app.name, "com.visualstudio.code")
        self.assertEqual(app.version, "1.105.1.176")
        self.assertEqual(app.variant, "x86_64/binary")
        self.assertEqual(app.origin_kind.value, "repo")
        self.assertEqual(app.origin_repos, ["linglong main"])
        self.assertEqual(app.size_mb, 426.5)            # 447261440 bytes
        self.assertEqual(app.executables, ["vscode"])
        self.assertEqual(app.extra["kind"], "app")
        self.assertEqual(app.extra["base"], "org.deepin.base/23.1.0")
        self.assertTrue(app.first_install.startswith("2026-"))

    def test_base_layer_is_runtime_kind(self):
        cfg = replace(CFG, linyap_repo_dir="/nonexistent")
        with mock.patch("pkgtool.backends.linyap.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=REAL_LIST_JSON)
            recs = run_backend(cfg)
        self.assertEqual(recs[1].name, "org.deepin.base")
        self.assertEqual(recs[1].extra["kind"], "runtime")

    def test_cli_failure_falls_back_to_states(self):
        with tempfile.TemporaryDirectory() as d:
            states = Path(d, "states.json")
            states.write_text(json.dumps(
                {"layers": [{"id": "org.x.app", "version": "1.0",
                             "arch": ["x86_64"], "channel": "main",
                             "module": "binary", "kind": "app",
                             "size": 1024 * 1024}]}), encoding="utf-8")
            cfg = replace(CFG, linyap_repo_dir=d)
            with mock.patch("pkgtool.backends.linyap.subprocess.run") as run:
                run.side_effect = OSError("no ll-cli")
                recs = run_backend(cfg)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].name, "org.x.app")
            self.assertEqual(recs[0].size_mb, 1.0)

    def test_bad_entries_skipped(self):
        cfg = replace(CFG, linyap_repo_dir="/nonexistent")
        bad = json.dumps([{"no_id": True}, {"id": "ok.app", "version": "1.0"},
                          {"id": "-bad name", "version": "1.0"}])
        with mock.patch("pkgtool.backends.linyap.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=bad)
            recs = run_backend(cfg)
        self.assertEqual([r.name for r in recs], ["ok.app"])


class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = replace(CFG, linyap_repo_dir="/nonexistent")
        with mock.patch("pkgtool.backends.linyap.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=REAL_LIST_JSON)
            cls.recs = run_backend(cfg)
        cls.app = next(r for r in cls.recs if r.extra["kind"] == "app")
        cls.base = next(r for r in cls.recs if r.extra["kind"] == "runtime")

    def test_classify(self):
        self.assertEqual(classify(self.app)[0], PkgClass.APP)
        cls_, reason = classify(self.base)
        self.assertEqual(cls_, PkgClass.LIBRARY)
        self.assertIn("运行时", reason)
        self.assertTrue(is_user_installed(self.app))
        self.assertFalse(is_user_installed(self.base))

    def test_launch(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ll-cli"):
            self.assertEqual(launch_plan(self.app, CFG),
                             ["ll-cli", "run", "com.visualstudio.code"])
        self.assertIsNone(launch_plan(self.base, CFG))   # 运行时不可启动

    def test_upgrade(self):
        argv, privileged = upgrade_plan(self.app)
        self.assertEqual(argv, ["ll-cli", "upgrade", "com.visualstudio.code"])
        self.assertTrue(privileged)

    def test_uninstall_steps(self):
        self.assertEqual(_linyap_steps(self.app, self.app.name),
                         [["ll-cli", "uninstall", "com.visualstudio.code"]])


if __name__ == "__main__":
    unittest.main()
