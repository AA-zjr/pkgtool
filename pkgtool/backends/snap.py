"""pkgtool.backends.snap — snap 包识别。

数据源（优先文件系统，离线可用、无需 root）：
  /var/lib/snapd/snaps/<name>_<rev>.snap   已下载的每个修订版本（持久缓存）
  /snap/<name>/current -> <rev>            当前激活的修订
  /snap/<name>/<rev>/bin/                  包内可执行文件
  /snap/<name>.<app>                       /snap/bin 下的全局命令入口
增强（失败自动降级）：
  snap list —— 人类可读版本、追踪频道、发布者。表头随 locale 变，
  所以按列位置解析，并要求第 3 列是纯数字修订号才认这一行。
"""
import glob
import os
import re
import subprocess

from ..base import Backend, OriginKind, PackageRecord
from ..config import CFG

_SNAP_FILE_RE = re.compile(r"(.+)_(\d+)\.snap$")
_STORE_LABEL = "snap-store"


class SnapBackend(Backend):
    pkg_type = "snap"

    @classmethod
    def available(cls, cfg=CFG):
        return os.path.isdir(cfg.snap_store_dir)

    def _list_info(self):
        """解析 `snap list` 表格 → {包名: (版本, 修订, 追踪, 发布者)}。"""
        try:
            out = subprocess.run(["snap", "list"], capture_output=True, text=True,
                                 timeout=self.cfg.timeout_query).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        info = {}
        for line in out.splitlines():
            cols = line.split()
            # 位置: [名称, 版本, 修订, 追踪, (发布者), (注记)] —— 与表头语言无关
            if len(cols) >= 4 and cols[2].isdigit() and not cols[0][0].isupper():
                info[cols[0]] = (cols[1], cols[2], cols[3],
                                 cols[4] if len(cols) > 4 else "")
        return info

    def _stored_revisions(self):
        """→ {包名: {修订号: .snap 文件路径}}"""
        stored = {}
        for f in glob.glob(os.path.join(self.cfg.snap_store_dir, "*.snap")):
            m = _SNAP_FILE_RE.match(os.path.basename(f))
            if m:
                stored.setdefault(m.group(1), {})[m.group(2)] = f
        return stored

    def _active_revision(self, name, revs):
        """/snap/<name>/current 符号链接指向的修订；链接缺失时取最高修订兜底。"""
        try:
            tgt = os.readlink(os.path.join(self.cfg.snap_mount_dir, name, "current"))
            if tgt.isdigit() and tgt in revs:
                return tgt
        except OSError:
            pass
        return max(revs, key=int)

    def _executables(self, name, rev):
        cfg = self.cfg
        exes = []
        bin_dir = os.path.join(cfg.snap_mount_dir, name, rev, "bin")
        if os.path.isdir(bin_dir):
            for e in sorted(os.listdir(bin_dir)):
                if not os.path.isdir(os.path.join(bin_dir, e)):
                    exes.append(f"{cfg.snap_mount_dir}/{name}/current/bin/{e}")
        for w in sorted(glob.glob(os.path.join(cfg.snap_mount_dir, "bin", "*"))):
            wn = os.path.basename(w)
            if wn == name or wn.startswith(name + "."):
                exes.append(w)
        return exes

    def collect(self):
        info = self._list_info()
        records = []
        for name, revs in sorted(self._stored_revisions().items()):
            active = self._active_revision(name, revs)
            version, tracking, publisher = "?", "", ""
            row = info.get(name)
            if row and row[1] == active:
                version, _rev, tracking, publisher = row

            extra = {"revision": active,
                     "stored_revs": ",".join(sorted(revs, key=int)),
                     "snap_file": revs[active]}
            if tracking:
                extra["tracking"] = tracking
            if publisher:
                extra["publisher"] = publisher
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=name, version=version,
                # 有追踪频道 = 来自 snap store 且能自动刷新；否则只是本地缓存目录里的文件
                origin_kind=OriginKind.REPO if tracking else OriginKind.LOCAL,
                origin_repos=[f"{_STORE_LABEL} {tracking}".strip()] if tracking else [],
                install_path=f"{self.cfg.snap_mount_dir}/{name}",
                executables=self._executables(name, active), extra=extra))
        return records
