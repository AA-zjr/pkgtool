"""pkgtool.flatpak_backend — flatpak 包识别。

数据源（优先文件系统，元数据就在安装目录里）：
  <instance>/app/<app-id>/<arch>/<branch>/<commit>/metadata   —— INI 格式元数据
      [Application] name/runtime/command/... （注意：通常没有 version 字段！）
  <commit>/export/bin/          —— 该 app 暴露的命令
  <commit>/files/               —— 实际文件负载
实例位置：system=/var/lib/flatpak   user=~/.local/share/flatpak
增强（失败自动降级，版本号只能从这里拿）：
  flatpak list --app/--runtime --columns=application,version,name,arch,branch,installation,origin
"""
import configparser
import glob
import os
import subprocess

from .base import Backend, PackageRecord

SYSTEM_DIR = "/var/lib/flatpak"
USER_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "flatpak")


def _flatpak_list(kind):
    """flatpak list --app|--runtime --columns=... -> [(id, version, name, arch, branch, install, origin)]。"""
    try:
        out = subprocess.run(
            ["flatpak", "list", f"--{kind}",
             "--columns=application,version,name,arch,branch,installation,origin"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 7 and cols[0]:
            rows.append(tuple(c.strip() for c in cols[:7]))
    return rows


def _parse_metadata(path):
    """读 flatpak metadata 的 [Application] 段。"""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(path, encoding="utf-8", errors="replace")
    except Exception:
        return {}
    return dict(cp["Application"]) if cp.has_section("Application") else {}


class FlatpakBackend(Backend):
    pkg_type = "flatpak"

    @classmethod
    def available(cls):
        return (os.path.isdir(os.path.join(SYSTEM_DIR, "app")) or
                os.path.isdir(os.path.join(USER_DIR, "app")))

    def _instances(self):
        for label, d in (("system", SYSTEM_DIR), ("user", USER_DIR)):
            if os.path.isdir(os.path.join(d, "app")):
                yield label, d

    def collect(self):
        records = []
        # CLI 增强数据（版本/来源），(id, arch, branch) -> (version, name, origin)
        cli_app = {(r[0], r[3], r[4]): (r[1], r[2], r[6]) for r in _flatpak_list("app")}
        cli_rt = {(r[0], r[3], r[4]): (r[1], r[2], r[6]) for r in _flatpak_list("runtime")}

        for inst_label, base in self._instances():
            exports_bin = os.path.join(base, "exports", "bin") if inst_label == "user" \
                else "/usr/lib/flatpak/exports/bin"
            for kind, cli_db in (("app", cli_app), ("runtime", cli_rt)):
                app_dir = os.path.join(base, kind)
                if not os.path.isdir(app_dir):
                    continue
                for meta in glob.glob(os.path.join(app_dir, "*/*/*/*/metadata")):
                    commit_dir = os.path.dirname(meta)
                    if os.path.islink(commit_dir):
                        continue  # active/current 是回指真实 commit 目录的符号链接，跳过避免重复
                    parts = meta.split(os.sep)          # .../app/<id>/<arch>/<branch>/<commit>/metadata
                    app_id, arch, branch = parts[-5], parts[-4], parts[-3]
                    md = _parse_metadata(meta)
                    key = (app_id, arch, branch)
                    ver, disp_name, origin = cli_db.get(key, ("?", "", ""))
                    if not origin:
                        origin = f"flatpak({inst_label})"

                    # 可执行：commit/export/bin + exports/bin 里匹配 app-id/command 的
                    cmd = md.get("command", "")
                    exes = []
                    exp_bin = os.path.join(commit_dir, "export", "bin")
                    if os.path.isdir(exp_bin):
                        exes += [os.path.join(exp_bin, e) for e in sorted(os.listdir(exp_bin))]
                    if os.path.isdir(exports_bin):
                        for e in sorted(os.listdir(exports_bin)):
                            if e in (app_id, cmd):
                                exes.append(os.path.join(exports_bin, e))

                    extra = {"kind": kind, "installation": inst_label,
                             "arch": arch, "branch": branch,
                             "commit": commit_dir.split(os.sep)[-1][:12]}
                    if disp_name:
                        extra["display_name"] = disp_name
                    if md.get("runtime"):
                        extra["runtime"] = md["runtime"]
                    records.append(PackageRecord(
                        pkg_type=self.pkg_type + ("-runtime" if kind == "runtime" else ""),
                        name=app_id, version=ver or "?", origin=origin,
                        install_path=os.path.join(commit_dir, "files"),
                        executables=exes, extra=extra))
        return records