"""pkgtool.backends.linyap — 如意玲珑（linyaps）应用识别。

数据源（离线、无需 root）：
  ll-cli list --json            已安装应用与 base/runtime 的完整元数据
  <repo>/states.json 的 layers  CLI 不可用时的降级来源（字段与上同构）

实测与社区反馈的坑点，这里逐条处理：
  · ll-cli 不带 --json 的表格输出会漏条目（本机 VSCode + base 两层只有
    --json 吐出来了），所以只用 --json，表格一概不解析
  · base 层的 kind 也是 "runtime"，区分应用与运行时只看 kind，不看名字
  · ref 完整形态是 渠道:ID/版本/架构/模块；应用与 deb/snap 同名很常见
    （firefox 三格式都有），变体里保留架构与模块
  · 同一应用多版本可能并存（升级后旧版未清），同变体键取先出现的，
    避免升级期间两条记录互相顶掉
"""
import json
import os
import subprocess
import time

from ..base import (BYTES_PER_MB, Backend, OriginKind, PackageRecord,
                    is_safe_name)
from ..config import CFG


def _list_json(cfg):
    """ll-cli list --json → [PackageInfo dict]；任何失败返回 []。"""
    try:
        p = subprocess.run(["ll-cli", "list", "--json"],
                           capture_output=True, text=True,
                           timeout=cfg.timeout_query)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _states_layers(cfg):
    """states.json 的 layers 数组，CLI 失败时的降级来源。"""
    path = os.path.join(cfg.linyap_repo_dir, "states.json")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    layers = data.get("layers") if isinstance(data, dict) else None
    if not isinstance(layers, list):
        return []
    # 字段结构未经真实数据核实，只接受长得像 PackageInfo 的条目
    return [x for x in layers
            if isinstance(x, dict) and x.get("id") and x.get("version")]


def _first_install(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class LinyapBackend(Backend):
    pkg_type = "linyap"

    @classmethod
    def available(cls, cfg=CFG):
        return (os.path.isdir(cfg.linyap_repo_dir)
                or bool(cls._which_ll_cli()))

    @staticmethod
    def _which_ll_cli():
        import shutil
        return shutil.which("ll-cli")

    def collect(self):
        items = _list_json(self.cfg) or _states_layers(self.cfg)
        records = []
        for it in items:
            app_id = it.get("id")
            version = it.get("version") or "?"
            if not app_id or not is_safe_name(str(app_id)):
                continue
            archs = it.get("arch") or []
            module = it.get("module") or ""
            arch = archs[0] if archs else "?"
            extra = {"kind": it.get("kind") or "",
                     "channel": it.get("channel") or "",
                     "module": module,
                     "arch": ",".join(str(a) for a in archs)}
            if it.get("base"):
                extra["base"] = it["base"]
            command = [str(c) for c in (it.get("command") or [])]
            if command:
                extra["command"] = " ".join(command)
            records.append(PackageRecord(
                pkg_type=self.pkg_type, name=str(app_id), version=version,
                variant=f"{arch}/{module or '?'}",
                origin_kind=(OriginKind.REPO if it.get("channel")
                             else OriginKind.LOCAL),
                origin_repos=(["linglong " + it["channel"]]
                              if it.get("channel") else []),
                size_mb=round(float(it.get("size") or 0) / BYTES_PER_MB, 1),
                executables=command,
                first_install=_first_install(it.get("install_time")),
                extra=extra))
        return records
