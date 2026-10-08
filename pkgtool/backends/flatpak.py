"""pkgtool.backends.flatpak — flatpak 包识别。

数据源（优先文件系统，元数据就在安装目录里）：
  <instance>/app/<app-id>/<arch>/<branch>/<commit>/metadata   INI 格式元数据
      [Application] name/runtime/command/...（注意：通常没有 version 字段）
  <commit>/export/bin/   该 app 暴露的命令
  <commit>/files/        实际文件负载
实例位置：system=/var/lib/flatpak   user=~/.local/share/flatpak
增强（失败自动降级，版本号只能从这里拿）：
  flatpak list --app/--runtime --columns=...
远端更新检测（需要联网，默认不做，见 check_updates）：
  flatpak remote-ls --updates
"""
import configparser
import glob
import os
import re
import subprocess

from ..base import Backend, OriginKind, PackageRecord, file_size_mb
from ..config import CFG

# app-id 允许下划线：app.zen_browser.zen 这类很常见，漏掉它会让这些应用
# 的更新检测被静默跳过（正则不匹配就当成非法行丢弃）
_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*\.[A-Za-z0-9_\-]+$")
_COLUMNS = "application,version,name,arch,branch,installation,origin"


def _flatpak_list(kind, cfg):
    """flatpak list --app|--runtime → [(id, version, name, arch, branch, install, origin)]。
    显式指定 --columns，避免默认列序随版本/locale 变化。"""
    try:
        out = subprocess.run(
            ["flatpak", "list", f"--{kind}", f"--columns={_COLUMNS}"],
            capture_output=True, text=True, timeout=cfg.timeout_query).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if len(cols) >= 7 and cols[0]:
            rows.append(tuple(cols[:7]))
    return rows


def _parse_metadata(path):
    """读 flatpak metadata 的 [Application] 段。
    注意用 read_file 而不是 read：read() 不接受 errors=，原实现传了这个参数，
    TypeError 又被 except Exception 静默吞掉，结果每个 metadata 都解析成空 dict。"""
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            cp.read_file(fh)
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    return dict(cp["Application"]) if cp.has_section("Application") else {}


def check_updates(cfg=CFG, timeout=90):
    """有新版的 flatpak 应用 → {app_id: 显示名}。
    需要联网（每个 installation 约 2 秒），所以默认不调用，由 --check-updates 触发。

    两个 installation 都要查：不带作用域时 flatpak 只看默认（系统）安装，
    用户级装的应用有新版会被整个漏掉。"""
    out = {}
    for scope in ("--system", "--user"):
        try:
            p = subprocess.run(["flatpak", "remote-ls", "--updates", scope,
                                "--columns=name,application"],
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in p.stdout.splitlines():
            cols = [c.strip() for c in line.split("\t")]
            if len(cols) >= 2 and _APP_ID_RE.match(cols[1]):
                out[cols[1]] = cols[0]
    return out


class FlatpakBackend(Backend):
    pkg_type = "flatpak"
    runtime_pkg_type = "flatpak-runtime"

    @classmethod
    def available(cls, cfg=CFG):
        return (os.path.isdir(os.path.join(cfg.flatpak_system_dir, "app"))
                or os.path.isdir(os.path.join(cfg.flatpak_user_dir, "app")))

    def _instances(self):
        for label, d in (("system", self.cfg.flatpak_system_dir),
                         ("user", self.cfg.flatpak_user_dir)):
            if os.path.isdir(os.path.join(d, "app")):
                yield label, d

    def collect(self):
        cfg = self.cfg
        cli_app = {(r[0], r[3], r[4]): r for r in _flatpak_list("app", cfg)}
        cli_rt = {(r[0], r[3], r[4]): r for r in _flatpak_list("runtime", cfg)}
        records = []
        for inst_label, base in self._instances():
            exports_bin = (os.path.join(base, "exports", "bin") if inst_label == "user"
                           else cfg.flatpak_exports_bin)
            for kind, cli_db in (("app", cli_app), ("runtime", cli_rt)):
                app_dir = os.path.join(base, kind)
                if not os.path.isdir(app_dir):
                    continue
                for meta in glob.glob(os.path.join(app_dir, "*/*/*/*/metadata")):
                    records.append(self._record(meta, kind, inst_label,
                                                cli_db, exports_bin))
        return [r for r in records if r is not None]

    def _record(self, meta, kind, inst_label, cli_db, exports_bin):
        commit_dir = os.path.dirname(meta)
        # active/current 是回指真实 commit 目录的符号链接，跳过避免重复计数
        if os.path.islink(commit_dir):
            return None
        parts = meta.split(os.sep)      # .../app/<id>/<arch>/<branch>/<commit>/metadata
        app_id, arch, branch = parts[-5], parts[-4], parts[-3]
        md = _parse_metadata(meta)
        row = cli_db.get((app_id, arch, branch))
        version, disp_name, origin = (row[1], row[2], row[6]) if row else ("?", "", "")

        cmd = md.get("command", "")
        exes = []
        exp_bin = os.path.join(commit_dir, "export", "bin")
        if os.path.isdir(exp_bin):
            exes += [os.path.join(exp_bin, e) for e in sorted(os.listdir(exp_bin))]
        if os.path.isdir(exports_bin):
            for e in sorted(os.listdir(exports_bin)):
                if e in (app_id, cmd):
                    exes.append(os.path.join(exports_bin, e))

        extra = {"kind": kind, "installation": inst_label, "arch": arch,
                 "branch": branch, "commit": os.path.basename(commit_dir)[:12]}
        if disp_name:
            extra["display_name"] = disp_name
        if md.get("runtime"):
            extra["runtime"] = md["runtime"]
        return PackageRecord(
            pkg_type=self.runtime_pkg_type if kind == "runtime" else self.pkg_type,
            name=app_id, version=version or "?",
            # 同一 app-id 可以并存多个 arch/branch（如 GL.default 的 25.08 与
            # 25.08-extra），不设变体维度它们会撞 key，按名字定位时无法区分
            variant=f"{arch}/{branch}",
            origin_kind=OriginKind.REPO if origin else OriginKind.LOCAL,
            origin_repos=[origin] if origin else [f"flatpak({inst_label})"],
            size_mb=file_size_mb(os.path.join(commit_dir, "files")),
            install_path=os.path.join(commit_dir, "files"),
            executables=exes, extra=extra)
