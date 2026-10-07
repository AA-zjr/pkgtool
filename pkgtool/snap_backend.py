"""pkgtool.snap_backend — snap 包识别。

数据源（按用户要求优先文件系统/缓存目录）：
  /var/lib/snapd/snaps/<name>_<rev>.snap   —— 已下载的每个修订版本（持久缓存）
  /snap/<name>/current -> <rev>            —— 当前激活的修订
  /snap/<name>/<rev>/bin/                  —— 包内可执行文件
  /snap/bin/<wrapper>                      —— 全局命令入口（名字=包名或 包名.应用名）
增强（失败自动降级）：
  snap list                                —— 人类可读版本、追踪频道、发布者
  （表头随 locale 变，所以按列位置解析；第3列必须是纯数字修订号才认）
"""
import glob
import os
import re
import subprocess

from .base import Backend, PackageRecord

SNAPS_DIR = "/var/lib/snapd/snaps"
SNAP_MOUNT = "/snap"


def _snap_list_info(timeout=30):
    """解析 `snap list` 表格 -> {包名: (版本, 修订, 追踪, 发布者)}。失败返回 {}。"""
    try:
        out = subprocess.run(["snap", "list"], capture_output=True, text=True,
                             timeout=timeout).stdout
    except Exception:
        return {}
    info = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[2].isdigit() and not cols[0][0].isupper():
            # 位置: [名称, 版本, 修订, 追踪, (发布者), (注记)] —— 与表头语言无关
            info[cols[0]] = (cols[1], cols[2], cols[3] if len(cols) > 3 else "",
                             cols[4] if len(cols) > 4 else "")
    return info


class SnapBackend(Backend):
    pkg_type = "snap"

    @classmethod
    def available(cls):
        return os.path.isdir(SNAPS_DIR)

    def collect(self):
        records = []
        if not os.path.isdir(SNAPS_DIR):
            return records
        info = _snap_list_info()

        # 每个 snap 名 -> {rev: .snap文件}
        stored = {}
        for f in glob.glob(os.path.join(SNAPS_DIR, "*.snap")):
            m = re.match(r"(.+)_(\d+)\.snap$", os.path.basename(f))
            if m:
                stored.setdefault(m.group(1), {})[m.group(2)] = f

        for name, revs in sorted(stored.items()):
            # 激活修订：/snap/<name>/current 符号链接
            cur_link = os.path.join(SNAP_MOUNT, name, "current")
            active = None
            try:
                tgt = os.readlink(cur_link)
                if tgt.isdigit() and tgt in revs:
                    active = tgt
            except OSError:
                pass
            if active is None:
                active = max(revs)  # 链接缺失时取最高修订兜底

            ver, tracking, publisher = "?", "", ""
            if name in info and info[name][1] == active:
                ver, _, tracking, publisher = info[name]

            origin = f"snap store({tracking})" if tracking else "snap(本地缓存目录)"

            # 可执行文件：包内 bin/ + /snap/bin 下名字匹配的包装器
            exes = []
            bin_dir = os.path.join(SNAP_MOUNT, name, active, "bin")
            if os.path.isdir(bin_dir):
                for e in sorted(os.listdir(bin_dir)):
                    p = os.path.join(bin_dir, e)
                    if not os.path.isdir(p):
                        exes.append(f"/snap/{name}/current/bin/{e}")
            for w in sorted(glob.glob(os.path.join(SNAP_MOUNT, "bin", "*"))):
                wn = os.path.basename(w)
                if wn == name or wn.startswith(name + "."):
                    exes.append(w)

            extra = {"revision": active, "stored_revs": ",".join(sorted(revs, key=int)),
                     "snap_file": revs[active]}
            if publisher:
                extra["publisher"] = publisher
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=name, version=ver, origin=origin,
                install_path=f"/snap/{name}", executables=exes, extra=extra))
        return records