"""pkgtool.apt.logs — dpkg.log / apt history.log 时间线解析。

安装通道判定（base.Channel）的证据来源。合并原先两套 apt history 解析：
deb_backend.parse_apt_history()（返回 {名: [(时间,版本)]}）和
update_channels.apt_history_names()（返回名字集合，且用 split(", ") 拆条目，
遇到版本括号内的逗号就拆错——靠正则补救）。这里统一成一个解析器。

数据来源可信度：
  /var/log/dpkg.log*        所有 dpkg 操作的时间线，含 dpkg -i，最接近事实
  /var/log/apt/history.log* 仅 apt 系工具的操作记录（apt install ./x.deb 也在里面）
两者都会轮转，查无记录时只能标 UNKNOWN，不能当成"没装过"。
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta

from ..compress import iter_lines
from ..config import CFG

TS_FMT = "%Y-%m-%d %H:%M:%S"

# dpkg.log: "2024-01-01 12:00:00 install bash:amd64 <none> 5.2-2ubuntu1"
# 第 3 字段是安装前版本（新装时为 <none>），第 4 字段才是装上的版本。
# 原实现取第 3 字段当版本号，拿到的一律是 "<none>"（好在只用了时间戳）。
_DPKG_INSTALL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) install (\S+) (\S+)(?: (\S+))?")

# apt history: "Install: libfoo:amd64 (1.2-3, automatic), bar:amd64 (4.5)"
# 架构段可有可无，括号内可能带 ", automatic"，所以不能用逗号拆条目。
_APT_ENTRY_RE = re.compile(r"([\w.+\-~]+)(?::[\w\-]+)?\s*\(([^)]*)\)")


def parse_ts(text):
    """→ datetime；解析不了返回 None（不抛）。"""
    try:
        return datetime.strptime(" ".join((text or "").split()), TS_FMT)
    except (ValueError, TypeError):
        return None


def dpkg_installs(cfg=CFG):
    """→ ({包名: (首次 install 时间, 版本)}, 全局最早事件时间)。
    只取每个包的第一次 install 事件；earliest 用于推算系统"出生时间"。"""
    first = {}
    earliest = None
    for line in iter_lines([cfg.dpkg_log_glob]):
        m = _DPKG_INSTALL_RE.match(line)
        if not m:
            continue
        ts, pkg, _old, new = m.groups()
        pkg = pkg.split(":")[0]
        if earliest is None or ts < earliest:
            earliest = ts
        if pkg not in first:
            first[pkg] = (ts, new or "")
    return first, earliest


def apt_history(cfg=CFG):
    """→ {包名: [(时间, 版本字符串), ...]}，来自 Install/Upgrade 行。
    括号里的 ", automatic" 是 apt 的自动安装标记不是版本的一部分，
    剥掉（Upgrade 行的 "旧版, 新版" 双版本则保留）。"""
    out = defaultdict(list)
    cur_ts = ""
    for line in iter_lines([cfg.apt_history_glob]):
        if line.startswith("Start-Date:"):
            cur_ts = " ".join(line.split(":", 1)[1].split())
        elif line.startswith(("Install:", "Upgrade:")):
            for name, ver in _APT_ENTRY_RE.findall(line):
                parts = [p.strip() for p in ver.split(",")]
                if parts and parts[-1] == "automatic":
                    parts = parts[:-1]
                out[name].append((cur_ts, ", ".join(parts)))
    return dict(out)


def birth_cutoff(earliest_ts, cfg=CFG):
    """系统"出生时间"上界 = 日志最早事件 + birth_margin_hours。
    在此窗口内装入且无 apt 记录的包，判为镜像自带。日志缺失时返回 None。"""
    start = parse_ts(earliest_ts)
    if start is None:
        return None
    return start + timedelta(hours=cfg.birth_margin_hours)
