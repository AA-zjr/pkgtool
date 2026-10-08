"""pkgtool.launch — 启动入口选择的测试。

两块重点：
  1. desktop 文件的适应度：Exec 的字段码、引号路径（带空格）、参数、
     %% 转义、损坏的 Exec；多个 desktop 文件时的主入口挑选规则
     （同包名 > 非 url-handler > 字母序）
  2. 常规安装布局：/opt + desktop（Electron 类）、/usr/bin 裸命令、
     只有辅助服务的包、数据/仓库包（无入口）、snap 旧修订入口、
     flatpak 应用与 runtime、AppImage 本体

全程不碰真实系统：desktop 文件写进临时目录后用 XDG 环境变量指过去，
snap/flatpak 的 which 探测用 mock 固定。
"""
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pkgtool.base import PackageRecord
from pkgtool.config import CFG
from pkgtool.launch import _parse_exec, _pick_exe, plan


def rec(**kw):
    defaults = dict(pkg_type="deb", name="app", version="1.0", extra={})
    defaults.update(kw)
    return PackageRecord(**defaults)


class TestParseExec(unittest.TestCase):
    def test_field_codes_dropped(self):
        self.assertEqual(_parse_exec("/opt/App/app %U"), ["/opt/App/app"])
        self.assertEqual(_parse_exec("app %f %F %u %i %c %k"), ["app"])

    def test_arguments_kept(self):
        self.assertEqual(_parse_exec("app --flag -x=1 %f"), ["app", "--flag", "-x=1"])

    def test_quoted_path_with_space(self):
        self.assertEqual(_parse_exec('"/opt/My App/app" %U'), ["/opt/My App/app"])

    def test_percent_escaping(self):
        self.assertEqual(_parse_exec("app --range=a%%b"), ["app", "--range=a%b"])

    def test_malformed_returns_empty(self):
        self.assertEqual(_parse_exec('app "unbalanced'), [])


class TestDesktopAdaptability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apps = Path(self._tmp.name, "applications")
        self.apps.mkdir()
        env = {"XDG_DATA_DIRS": self._tmp.name,
               "XDG_DATA_HOME": str(Path(self._tmp.name, "user"))}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_desktop(self, desktop_id, exec_line):
        p = self.apps / f"{desktop_id}.desktop"
        p.write_text(f"[Desktop Entry]\nType=Application\nExec={exec_line}\n",
                     encoding="utf-8")
        return p

    def test_opt_layout_absolute_exec(self):
        self.write_desktop("app", "/opt/App/app %U")
        argv = plan(rec(extra={"desktop_id": "app"}), CFG)
        self.assertEqual(argv, ["/opt/App/app"])

    def test_quoted_exec_with_space(self):
        # 布局：/opt/My App/app（目录名带空格，Exec 必须带引号）
        self.write_desktop("myapp", '"/opt/My App/app" %U')
        argv = plan(rec(name="myapp", extra={"desktop_id": "myapp"}), CFG)
        self.assertEqual(argv, ["/opt/My App/app"])

    def test_bare_command_exec(self):
        self.write_desktop("app", "app %f")
        argv = plan(rec(extra={"desktop_id": "app"}), CFG)
        self.assertEqual(argv, ["app"])

    def test_url_handler_deprioritized(self):
        # qoder-cn 布局：主程序 desktop + url-handler 辅助项，必须挑主入口
        self.write_desktop("qoder-cn-url-handler", "handler %U")
        self.write_desktop("qoder-cn", "main %U")
        r = rec(name="qoder-cn",
                extra={"desktop_files": ["qoder-cn-url-handler", "qoder-cn"]})
        self.assertEqual(plan(r, CFG), ["main"])

    def test_prefers_name_matching_desktop(self):
        self.write_desktop("foo-helper", "helper %U")
        self.write_desktop("foo", "main %U")
        r = rec(name="foo", extra={"desktop_files": ["foo-helper", "foo"]})
        self.assertEqual(plan(r, CFG), ["main"])

    def test_desktop_id_with_space(self):
        # "Clash Verge.desktop" 这类带空格的 ID
        self.write_desktop("Clash Verge", "clash-verge %U")
        argv = plan(rec(name="clash-verge", extra={"desktop_id": "Clash Verge"}), CFG)
        self.assertEqual(argv, ["clash-verge"])

    def test_broken_exec_falls_back_to_executables(self):
        self.write_desktop("app", 'broken "quote %U')
        argv = plan(rec(executables=["/usr/bin/app"],
                        extra={"desktop_id": "app"}), CFG)
        self.assertEqual(argv, ["/usr/bin/app"])

    def test_missing_desktop_falls_back_to_executables(self):
        argv = plan(rec(executables=["/usr/bin/app"],
                        extra={"desktop_id": "nonexistent"}), CFG)
        self.assertEqual(argv, ["/usr/bin/app"])

    def test_no_entry_at_all(self):
        self.assertIsNone(plan(rec(extra={}), CFG))          # 数据/仓库包
        self.assertIsNone(plan(rec(pkg_type="deb",
                                   extra={"loose": "/tmp/x.deb"}), CFG))


class TestPickExe(unittest.TestCase):
    def test_exact_name_match(self):
        r = rec(name="app", executables=["/usr/bin/applet", "/usr/bin/app"])
        self.assertEqual(_pick_exe(r), "/usr/bin/app")

    def test_desktop_id_match(self):
        r = rec(name="pkg", extra={"desktop_id": "realapp"},
                executables=["/usr/bin/pkg", "/usr/bin/realapp"])
        self.assertEqual(_pick_exe(r), "/usr/bin/realapp")

    def test_prefix_match(self):
        r = rec(name="firefox", executables=["/usr/bin/firefox-esr",
                                             "/usr/bin/firefox-driver"])
        self.assertEqual(_pick_exe(r), "/usr/bin/firefox-esr")

    def test_first_as_last_resort(self):
        r = rec(name="thing", executables=["/usr/bin/b", "/usr/bin/a"])
        self.assertEqual(_pick_exe(r), "/usr/bin/b")

    def test_empty(self):
        self.assertEqual(_pick_exe(rec(executables=[])), "")


class TestOtherFormats(unittest.TestCase):
    def test_snap_global_entry_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(CFG, snap_mount_dir=d)
            Path(d, "bin").mkdir()
            Path(d, "bin", "firefox").write_bytes(b"")
            self.assertEqual(plan(rec(pkg_type="snap", name="firefox"), cfg),
                             [str(Path(d, "bin", "firefox"))])

    def test_snap_run_fallback_and_base_rejected(self):
        with tempfile.TemporaryDirectory() as d:      # 空目录：没有 /snap/bin 入口
            cfg = replace(CFG, snap_mount_dir=d)
            with mock.patch("shutil.which", return_value="/usr/bin/snap"):
                self.assertEqual(plan(rec(pkg_type="snap", name="firefox"), cfg),
                                 ["snap", "run", "firefox"])
                self.assertIsNone(plan(rec(pkg_type="snap", name="bare"), cfg))
            with mock.patch("shutil.which", return_value=None):   # snap 不存在
                self.assertIsNone(plan(rec(pkg_type="snap", name="firefox"), cfg))

    def test_flatpak_app_vs_runtime(self):
        with mock.patch("shutil.which", return_value="/usr/bin/flatpak"):
            self.assertEqual(
                plan(rec(pkg_type="flatpak", name="com.brave.Browser",
                         extra={"kind": "app"}), CFG),
                ["flatpak", "run", "com.brave.Browser"])
            self.assertIsNone(plan(
                rec(pkg_type="flatpak", name="org.x.Runtime",
                    extra={"kind": "runtime"}), CFG))
            self.assertIsNone(plan(
                rec(pkg_type="flatpak-runtime", name="org.x.Runtime",
                    extra={"kind": "runtime"}), CFG))

    def test_appimage_layouts(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d, "CC-Switch.AppImage"))
            Path(p).write_bytes(b"x")
            # 已安置：found_at 指向本体
            self.assertEqual(plan(rec(pkg_type="appimage",
                                      extra={"found_at": p}), CFG), [p])
            # 散落在下载目录的也能启动
            self.assertEqual(plan(rec(pkg_type="appimage",
                                      extra={"loose": p}), CFG), [p])
            # 文件已不在：拒绝
            self.assertIsNone(plan(rec(pkg_type="appimage",
                                       extra={"found_at": "/nonexistent"}), CFG))


if __name__ == "__main__":
    unittest.main()
