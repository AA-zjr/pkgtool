"""pkgtool.safety.py — 卸载安全层：包分类 + 预览(dry-run) + 执行。

分类（只允许删除 app 类）——四层证据，保守默认判不准就是 system：
  1. dpkg priority/section（来自 apt 索引 Packages 文件）: required/important → base；
     section=libs/libdevel/kernel → library/system
  2. 命名规则: linux-*/firmware/nvidia-* → system；lib*/-dev/-dbg/python3-* → library
  3. 文件布局: 有 .desktop 或装进 /opt/ → app（自带软件）；完全无可执行文件 → library
  4. 安装意图: local-deb/unknown/manual 标记且带可执行文件 → app（用户主动装的）
其余一律 system（保守默认，不给删）。
"""
import glob
import os
import re
import shlex
import subprocess

from .base import user_home

CRITICAL = {
    "dpkg", "apt", "bash", "dash", "coreutils", "systemd", "init", "base-files",
    "base-passwd", "util-linux", "login", "passwd", "tar", "gzip", "sed", "grep",
    "findutils", "e2fsprogs", "kmod", "mount", "perl-base", "libc-bin", "debconf",
    "apt-utils", "policy-rc.d", "init-system-helpers",
}

CLASS_LABEL = {"base": "基础包", "library": "库/依赖", "system": "系统组件", "app": "软件"}
BLOCK_REASONS = {
    "base": "基础包，删除会破坏系统（dpkg/apt/bash 一类）",
    "library": "库/依赖，由软件自动管理，不应单独删除",
    "system": "系统组件，未通过“用户软件”判定，不给删",
}


def classify_deb(name, section="", priority="", exes=(), top_dirs=(),
                 has_desktop=False, channel="", apt_mark=""):
    """返回 (cls, reason)，cls ∈ base|library|system|app。"""
    if name in CRITICAL or priority in ("required", "important"):
        return "base", f"基础包(priority={priority or 'critical-list'})"
    if (name.startswith(("linux-", "xserver-", "firmware-")) or
            section in ("kernel", "base") or "firmware" in name):
        return "system", "内核/固件/X 服务"
    if has_desktop or any(d.startswith("opt/") for d in top_dirs) \
            or channel.startswith(("local-deb", "unknown")):
        return "app", ("GUI应用(.desktop)" if has_desktop
                       else "自带软件(/opt)" if any(d.startswith("opt/") for d in top_dirs)
                       else "用户自行安装")
    if section in ("libs", "libdevel"):
        return "library", f"section={section}"
    if re.match(r"^lib[a-z0-9]+.*\d", name) or name.endswith(("-dev", "-dbg")):
        return "library", "命名规则(lib*/-dev)"
    if not exes:
        return "library", "无可执行文件(库/数据/配置)"
    if apt_mark == "manual":
        return "app", "用户主动安装(manual标记)"
    return "system", "系统组件(保守默认)"


def classify_snap(name):
    if name in ("snapd", "bare") or name.startswith(("core", "gnome-", "gtk-common-themes", "mesa-")):
        return "base", "snap 基础运行时"
    return "app", "snap 应用"


def classify_flatpak(kind):
    if kind == "runtime":
        return "library", "flatpak runtime(运行库)"
    return "app", "flatpak 应用"


def _size_mb(path):
    total = 0
    try:
        if os.path.isfile(path) or os.path.islink(path):
            total = os.path.getsize(path)
        else:
            for dp, _dn, fn in os.walk(path):
                for f in fn:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
    except OSError:
        pass
    return round(total / 1048576, 1)


def find_residues(name, exes=(), desktop_id=""):
    """扫描用户可写区域，找与包标识匹配的残留目录/文件 → [(path, size_mb)]。
    匹配标识：包名、可执行文件名、.desktop 的 ID（精确/忽略大小写/前缀+连字符）。"""
    ids = {name}
    for e in exes:
        b = os.path.basename(e)
        if b and len(b) > 2:      # 过短的标识容易误伤
            ids.add(b)
    if desktop_id:
        ids.add(desktop_id)
    home = user_home()
    roots = [os.path.join(home, d) for d in
             (".config", ".cache", ".local/share", ".local/state",
              ".local/lib", ".local/bin")] + ["/opt"]
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(root, entry)
            stripped = entry.lstrip(".")  # 兼容 .wechat 这类点前缀目录
            if any(entry == i or entry.lower() == i.lower()
                   or stripped == i or stripped.lower() == i.lower()
                   or entry.startswith(i + "-")
                   or stripped.startswith(i + "-") for i in ids):
                out.append((full, _size_mb(full)))
    return out


def apt_cache_files(name):
    """apt 缓存里该包的 .deb（彻底清理时一并删掉）。"""
    pats = (f"/var/cache/apt/archives/{name}_*.deb",
            f"/var/cache/apt/archives/{name}_*.ddeb")
    return [p for pat in pats for p in glob.glob(pat)]


def build_deep_script(pkg_type, target, residues=(), deep=True, autoremove=True):
    """拼出以 root 执行的命令序列（一次 pkexec 弹一个密码框全部完成）。"""
    cmds = []
    if pkg_type == "deb":
        ar = "--autoremove " if autoremove else ""
        cmds.append(f"apt-get purge {ar}-y {shlex.quote(target)}")
        for f in apt_cache_files(target):
            cmds.append(f"rm -f {shlex.quote(f)}")
    elif pkg_type == "snap":
        cmds.append(f"snap remove --purge {shlex.quote(target)}")  # --purge 连保存数据一起删
    elif pkg_type == "flatpak":
        cmds.append(f"flatpak uninstall -y --delete-data {shlex.quote(target)}")
    elif pkg_type == "appimage":
        cmds.append(f"rm -f {shlex.quote(target)}")  # target 是文件路径
    if deep:
        for path, _sz in residues:
            cmds.append(f"rm -rf {shlex.quote(path)}")
    return cmds


def preview_remove(row):
    """dry-run + 残留扫描（无需 root）。row = UI 数据行 dict。"""
    pkg_type, name = row["pkg_type"], row["name"]
    target = row.get("install_path") or name if pkg_type == "appimage" else name
    exes = row.get("executables") or []
    desktop_id = (row.get("extra") or {}).get("desktop_id", "")
    try:
        if pkg_type == "deb":
            # 用 remove -s 解析（输出稳定的 Remv 行）；purge -s 只出本地化文本。
            # 移除的包集合两者一致，实际执行时才用 purge。
            p = subprocess.run(["apt-get", "remove", "-s", name],
                               capture_output=True, text=True, timeout=60)
            will = [l.split()[1] for l in p.stdout.splitlines() if l.startswith("Remv ")]
            ok, err = p.returncode == 0, p.stderr.strip()[-500:]
        else:
            will, ok, err = ([os.path.basename(target)] if pkg_type == "appimage" else [name]), True, ""
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "will_remove": [], "residues": [],
                "error": f"{type(e).__name__}: {e}"}
    residues = [(p_, s) for p_, s in find_residues(name, exes, desktop_id)]
    freed = round(sum(s for _p, s in residues), 1)
    return {"ok": ok, "will_remove": will, "error": err,
            "residues": [{"path": p_, "size_mb": s} for p_, s in residues],
            "freed_mb": freed,
            "command": " + ".join(build_deep_script(pkg_type, target, residues))}


def execute_remove(row, deep=True, autoremove=True, timeout=180):
    """pkexec bash -c 执行完整清理脚本（弹一次图形密码框）。"""
    pkg_type, name = row["pkg_type"], row["name"]
    target = row.get("install_path") or name if pkg_type == "appimage" else name
    residues = find_residues(name, row.get("executables") or [],
                             (row.get("extra") or {}).get("desktop_id", "")) \
        if deep else []
    script = " && ".join(build_deep_script(pkg_type, target, residues, deep, autoremove))
    try:
        p = subprocess.run(["pkexec", "bash", "-c", script],
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip()[-2000:]
        return {"ok": p.returncode == 0,
                "output": out or f"(exit={p.returncode})",
                "residues_removed": [p_ for p_, _s in residues],
                "command": "sudo bash -c '" + script.replace("'", "'\\''") + "'"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "等待密码超时（180s）——请手动执行下方命令",
                "residues_removed": [],
                "command": "sudo bash -c '" + script.replace("'", "'\\''") + "'"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "output": f"{type(e).__name__}: {e}",
                "residues_removed": [],
                "command": "sudo bash -c '" + script.replace("'", "'\\''") + "'"}