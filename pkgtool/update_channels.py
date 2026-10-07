"""更新通道：识别每个包的安装来源 → 决定升级路径。

deb: 在 apt 索引中，或出现在 /var/log/apt/history.log* 的 Install/Upgrade 行
     → 'apt'（用 apt 升级）；否则 'local-deb'（本地 .deb，走软件自带推送）。
flatpak: remote-ls --updates 检测 flathub 是否有新版。
"""
import bz2
import glob
import gzip
import lzma
import re
import subprocess


def _read_any(f):
    try:
        if f.endswith(".gz"):
            return gzip.open(f, "rb").read().decode("utf-8", "replace")
        if f.endswith(".xz"):
            return lzma.open(f, "rb").read().decode("utf-8", "replace")
        if f.endswith(".bz2"):
            return bz2.open(f, "rb").read().decode("utf-8", "replace")
        with open(f, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def apt_history_names():
    """/var/log/apt/history.log* 中出现过 Install/Upgrade 的包名（apt 管理过的证据，含已移除源）。"""
    names = set()
    files = ["/var/log/apt/history.log"] + sorted(glob.glob("/var/log/apt/history.log.*"))
    for f in files:
        data = _read_any(f)
        if not data:
            continue
        for m in re.finditer(r"^(?:Install|Upgrade): (.+)$", data, re.M):
            for part in m.group(1).split(", "):
                # 只认 "name:arch (ver...)" 形态，丢弃多版本行逗号拆出的版本号碎片
                mm = re.match(r"^(\S+)\s*\(", part.strip())
                if mm:
                    names.add(mm.group(1).split(":")[0])  # 去 :amd64
    return names


def deb_channels(installed_names):
    """→ {name: 'apt' | 'apt-local' | 'manual-deb'}。
    apt       = 在已配置仓库索引中（可用 apt 升级）
    apt-local = apt install ./xxx.deb 装过（历史有记录），但当前无源可更新
    manual-deb= dpkg -i 手动安装
    """
    from . import apt_repo
    try:
        idx = apt_repo.load_index()
    except Exception:  # noqa: BLE001
        idx = {}
    hist = apt_history_names()
    out = {}
    for n in installed_names:
        if n in idx:
            out[n] = "apt"
        elif n in hist:
            out[n] = "apt-local"
        else:
            out[n] = "manual-deb"
    return out


def flatpak_updates():
    """flatpak remote-ls --updates → [{name, app_id}]（需网络，约 2 秒；失败返回 []）。"""
    try:
        p = subprocess.run(["flatpak", "remote-ls", "--updates"],
                           capture_output=True, text=True, timeout=90)
        out = []
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z0-9\-]+$", parts[1]):
                out.append({"name": parts[0], "app_id": parts[1]})
        return out
    except Exception:  # noqa: BLE001
        return []