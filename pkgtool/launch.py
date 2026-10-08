"""pkgtool.launch — 从工具里启动已安装的应用。

各格式的启动方式不同，统一收敛在这里（CLI 与交互界面共用）：
  deb       用记录里的可执行文件（dpkg .list 里 bin_dirs 下的可执行项）
  snap      优先 /snap/bin/<name> 全局入口，退化到 snap run <name>
  flatpak   flatpak run <app-id>（runtime 不是应用，拒绝启动）
  appimage  应用本体就是文件，直接执行（散落在下载目录的也能启动）

启动必须是分离的：GUI 应用要长驻，pkgtool（尤其 curses 界面）不能被它
挂住，关掉工具也不该带走应用——所以 stdin/stdout/stderr 全部接到
devnull，并 start_new_session 让它自成会话。
"""
import configparser
import os
import re
import shlex
import shutil
import subprocess

from .apt import actions
from .classify import _SNAP_BASE_RE          # snap 基础运行时名单，判定逻辑同源
from .config import CFG

_FIELD_CODE_RE = re.compile(r"%[a-zA-Z]")


def _xdg_app_dirs(cfg):
    """desktop 文件的查找目录：XDG_DATA_DIRS + 用户目录（XDG_DATA_HOME）。"""
    dirs = [os.path.join(d, "applications") for d in cfg.xdg_data_dirs]
    dirs.append(os.path.join(cfg.xdg_data_home, "applications"))
    return dirs


def _parse_exec(line):
    """解析 Exec= → argv。按 desktop-entry 规范去掉 %f/%U 一类字段码
    （它们是给文件管理器传参的占位符，直接启动不该带上），%% 转义回
    字面 %。解析失败（引号不配对等）返回空列表，调用方退到下一来源。"""
    try:
        parts = shlex.split(line)
    except ValueError:
        return []
    return [t.replace("%%", "%")
            for t in parts if not _FIELD_CODE_RE.fullmatch(t)]


def _desktop_argv(rec, cfg):
    """从包自带的 .desktop 文件里取启动命令。
    Electron/Qt 类 .deb 的主程序几乎都装在 /opt/<应用>/ 下，/usr/bin 只留
    辅助服务（实测 clash-verge 的 6 个 bin 全是 service/mihomo），靠 dpkg
    文件清单按 bin_dirs 过滤根本找不到 GUI 本体——desktop 的 Exec= 是软件
    作者声明的真正入口，优先级应最高。
    包可能带多个 desktop 文件（主程序 + url-handler 等辅助项），按
    「与包名同源 > 非 url-handler > 字母序」挑主入口。"""
    ids = []
    did = rec.extra.get("desktop_id")
    if did:
        ids.append(did)
    ids += [d for d in rec.extra.get("desktop_files", []) if d not in ids]

    def rank(i):
        return (not (i == rec.name or i.startswith(rec.name + "-")
                     or rec.name.startswith(i)),
                "url-handler" in i.lower() or "handler" in i.lower(), i)

    for i in sorted(ids, key=rank):
        for d in _xdg_app_dirs(cfg):
            path = os.path.join(d, i + ".desktop")
            if not os.path.isfile(path):
                continue
            cp = configparser.ConfigParser(interpolation=None)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    cp.read_file(fh)
                exec_line = cp["Desktop Entry"]["Exec"]
            except (KeyError, configparser.Error, OSError):
                continue
            argv = _parse_exec(exec_line)
            if argv:
                return argv
    return []


def _pick_exe(rec):
    """从记录的可执行文件里挑最像"应用入口"的那个。
    优先级：desktop_id 精确 > 包名精确 > 前缀（firefox-esr 之于 firefox）
    > 第一个——desktop_id 是软件作者声明的应用标识，比包名更能代表入口。"""
    exes = rec.executables
    if not exes:
        return ""
    did = rec.extra.get("desktop_id")
    names = [n for n in (did, rec.name) if n]
    for n in names:
        for e in exes:
            if os.path.basename(e) == n:
                return e
    for e in exes:
        base = os.path.basename(e)
        if any(base.startswith(n) for n in names if len(n) >= 3):
            return e
    return exes[0]


def plan(rec, cfg=CFG):
    """→ 启动 argv；该记录没有可启动的可执行程序时返回 None。"""
    t = rec.pkg_type
    if t == "deb":
        argv = _desktop_argv(rec, cfg)
        if argv:
            return argv
        exe = _pick_exe(rec)
        return [exe] if exe else None
    if t == "snap":
        p = os.path.join(cfg.snap_mount_dir, "bin", rec.name)
        if os.path.exists(p):
            return [p]
        # bare/core/gnome-* 这类基础运行时没有应用入口，snap run 只会报错
        if shutil.which("snap") and not _SNAP_BASE_RE.match(rec.name):
            return ["snap", "run", rec.name]
        return None
    if t == "flatpak":
        # flatpak-runtime（pkg_type 为 flatpak-runtime 或 extra.kind=runtime）
        # 是运行库不是应用；flatpak run 对它也不会成功
        if t == "flatpak" and rec.extra.get("kind", "app") != "runtime" \
                and shutil.which("flatpak"):
            return ["flatpak", "run", rec.name]
        return None
    if t == "linyap":
        if rec.extra.get("kind", "app") in ("app", "") and shutil.which("ll-cli"):
            return ["ll-cli", "run", rec.name]
        return None
    if t == "appimage":
        path = rec.extra.get("found_at") or rec.extra.get("loose") \
            or rec.install_path
        if path and os.path.exists(path):
            return [path]
        return None
    return None


def launch(rec, cfg=CFG):
    """分离启动一个应用 → actions.Result。command 是实际启动的 argv。"""
    argv = plan(rec, cfg)
    if argv is None:
        if rec.pkg_class is not None and rec.pkg_class.value != "app":
            error = f"{rec.name} 不是应用（{rec.class_reason}），没有可启动的入口"
        else:
            error = "没有找到可用的可执行程序"
        return actions.Result(ok=False, error=error, command=[])
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as e:
        return actions.Result(ok=False, error=f"{type(e).__name__}: {e}",
                              command=argv)
    return actions.Result(ok=True, command=argv)
