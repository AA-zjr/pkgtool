#!/usr/bin/env python3
"""pkg_ui.py — 软件盘点浏览器可视化 UI（零第三方依赖，纯标准库）。

用法:
  cd "/home/zjr1236/list ii"
  python3 pkg_ui.py                 # 默认 http://0.0.0.0:8765
  python3 pkg_ui.py --port 9000     # 换端口
  python3 pkg_ui.py --host 127.0.0.1

WSL 提示：浏览器在 Windows 侧时直接开 http://localhost:8765（WSL2 自动转发）；
局域网其他机器访问用 `wsl hostname -I` 的 IP。
数据由 pkgtool 各后端采集（deb/snap/flatpak），结果缓存，页面右上角可手动刷新。
"""
import argparse
import glob as _glob
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pkgtool import discover
from pkgtool.base import user_home
from pkgtool.safety import (BLOCK_REASONS, classify_flatpak,
                            classify_snap, execute_remove, preview_remove)

STATE = {"data": None, "collecting": False, "error": None, "by_key": {}}
LOCK = threading.Lock()


UI_VERSION = 19  # 前端代码版本：页面加载时嵌入，服务端每次改 UI 就 +1


def collect():
    """后台采集所有后端，结果写入 STATE。"""
    with LOCK:
        if STATE["collecting"]:
            return
        STATE["collecting"] = True
        STATE["error"] = None
    t0 = time.time()
    try:
        rows, types = [], {}
        for be in discover():
            recs = be.collect()
            types[be.pkg_type] = len(recs)
            for r in recs:
                rows.append({
                    "pkg_type": r.pkg_type, "name": r.name, "version": r.version,
                    "origin": r.origin, "install_path": r.install_path,
                    "executables": r.executables, "first_install": r.first_install,
                    "extra": r.extra})
        # snap/flatpak 补分类（deb 在后端里已分），并建 (type,name) 索引供卸载接口用
        by_key = {}
        for row in rows:
            e = row["extra"]
            if "class" not in e:
                if row["pkg_type"] == "snap":
                    c, why = classify_snap(row["name"])
                else:
                    c, why = classify_flatpak(e.get("kind", "app"))
                e["class"], e["class_reason"] = c, why
            # 同名冲突（已安装包 vs 散落文件）时优先保留非 loose 记录
            key = (row["pkg_type"], row["name"])
            prev = by_key.get(key)
            if prev is None or (prev["extra"].get("loose") and not e.get("loose")):
                by_key[key] = row
        # Python 环境覆盖清单（含空壳，一眼看出哪些被嗅探到）
        try:
            from pkgtool.pip_backend import environment_summary
            py_envs = environment_summary()
        except Exception:  # noqa: BLE001
            py_envs = []
        # APT 可升级检测：已安装版本 vs 仓库 latest（无需 root）
        try:
            from pkgtool import apt_repo
            idx = apt_repo.load_index()
            apt_upg = [{"name": row["name"], "installed": row["version"], "candidate": e["latest"]}
                       for row in rows if row["pkg_type"] == "deb" and not row["extra"].get("loose")
                       for e in [idx.get(row["name"])] if e and apt_repo.ver_gt(e["latest"], row["version"])]
        except Exception:  # noqa: BLE001
            apt_upg = []
        # 更新通道：deb 标记安装来源（apt 管理 vs 本地 .deb）；flatpak 检测 flathub 新版
        flatpak_upg = []
        try:
            from pkgtool import update_channels
            ch = update_channels.deb_channels(
                [row["name"] for row in rows if row["pkg_type"] == "deb" and not row["extra"].get("loose")])
            for row in rows:
                if row["pkg_type"] == "deb" and not row["extra"].get("loose"):
                    row["extra"]["channel"] = ch.get(row["name"], "local-deb")
            flatpak_upg = update_channels.flatpak_updates()
        except Exception:  # noqa: BLE001
            pass
        with LOCK:
            STATE["by_key"] = by_key
            STATE["data"] = {"rows": rows, "meta": {
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed": round(time.time() - t0, 1), "types": types,
                "py_envs": py_envs, "apt_upgradable": apt_upg,
                "flatpak_updates": flatpak_upg, "version": UI_VERSION}}
    except Exception as e:  # noqa: BLE001
        with LOCK:
            STATE["error"] = f"{type(e).__name__}: {e}"
    finally:
        with LOCK:
            STATE["collecting"] = False


DL_JOBS = {}  # apt .deb 下载任务：id → {status, file, error}
APT_JOBS = {}  # apt 升级任务：id → {status, output[], exit_code, cancelled, proc}
FLATPAK_JOBS = {}  # flatpak 升级任务：同上结构
_SUDO_OK = None


def _sudo_nopass():
    """是否有免密 sudo（有则 UI 可直接执行升级，否则只能给命令）。结果缓存。"""
    global _SUDO_OK
    if _SUDO_OK is None:
        try:
            _SUDO_OK = subprocess.run(["sudo", "-n", "true"],
                                      capture_output=True, timeout=10).returncode == 0
        except Exception:  # noqa: BLE001
            _SUDO_OK = False
    return _SUDO_OK


def _run_apt_upgrade(job_id, name, version, password):
    """后台线程：sudo -S apt-get install，逐行回显输出；支持 killpg 中止。密码只走 stdin，不落盘不记日志。"""
    job = APT_JOBS[job_id]
    cmd = ["sudo", "-S", "-p", "", "apt-get", "install", "-y", f"{name}={version}"]
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        job["proc"] = p
        try:  # 先喂密码再读输出，避免互锁
            p.stdin.write(password + "\n")
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        for line in p.stdout:
            job["output"].append(line.rstrip("\n"))
            if len(job["output"]) > 400:
                del job["output"][0]
        p.wait()
        code = p.returncode
    except Exception as e:  # noqa: BLE001
        job["output"].append(f"{type(e).__name__}: {e}")
        code = -1
    if job.get("cancelled"):
        job.update(status="cancelled", exit_code=code)
    else:
        job.update(status="done" if code == 0 else "error", exit_code=code)


def _run_flatpak_update(job_id, ids, use_sudo, password):
    """后台线程：flatpak update -y（系统级安装加 sudo -S）；支持 killpg 中止。"""
    job = FLATPAK_JOBS[job_id]
    cmd = (["sudo", "-S", "-p", ""] if use_sudo else []) + ["flatpak", "update", "-y"] + ids
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        job["proc"] = p
        if use_sudo:
            try:
                p.stdin.write(password + "\n")
                p.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        for line in p.stdout:
            job["output"].append(line.rstrip("\n"))
            if len(job["output"]) > 400:
                del job["output"][0]
        p.wait()
        code = p.returncode
    except Exception as e:  # noqa: BLE001
        job["output"].append(f"{type(e).__name__}: {e}")
        code = -1
    if job.get("cancelled"):
        job.update(status="cancelled", exit_code=code)
    else:
        job.update(status="done" if code == 0 else "error", exit_code=code)


def _apt_download(job_id, name, version):
    """后台线程：apt-get download（非 root 可用）→ 存到 ~/Downloads。"""
    d = os.path.join(user_home(), "Downloads")  # pwd 取真实 home（$HOME 在沙箱里不可靠）
    try:
        os.makedirs(d, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="apt_dl_")
        p = subprocess.run(["apt-get", "download", f"{name}={version}"],
                           cwd=tmp, capture_output=True, text=True, timeout=900)
        debs = _glob.glob(os.path.join(tmp, "*.deb"))
        if p.returncode != 0 or not debs:
            lines = (p.stderr or p.stdout).strip().splitlines()
            DL_JOBS[job_id].update(status="error",
                                   error=(lines[-1] if lines else "下载失败")[:300])
            return
        dest = os.path.join(d, os.path.basename(debs[0]))
        shutil.move(debs[0], dest)
        DL_JOBS[job_id].update(status="done", file=dest)
    except Exception as e:  # noqa: BLE001
        DL_JOBS[job_id].update(status="error", error=f"{type(e).__name__}: {e}")


REQ_LOG = "/tmp/pkg_ui_req.log"

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 写请求日志（诊断用）：时间 + 方法 + 路径
        try:
            with open(REQ_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {self.command} {urlparse(self.path).path}\n")
        except OSError:
            pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/data":
            with LOCK:
                if STATE["collecting"]:
                    self._send(202, json.dumps({"status": "collecting"}))
                    return
                if STATE["error"]:
                    self._send(500, json.dumps({"error": STATE["error"]}, ensure_ascii=False))
                    return
                if not STATE["data"]:
                    self._send(202, json.dumps({"status": "collecting"}))
                    return
                self._send(200, json.dumps(STATE["data"], ensure_ascii=False))
        elif self.path.startswith("/api/apt/search"):
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            limit = min(int(parse_qs(urlparse(self.path).query).get("limit", ["30"])[0]), 60)
            try:
                from pkgtool import apt_repo
                self._send(200, json.dumps(apt_repo.search(q, limit), ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        elif self.path.startswith("/api/apt/info"):
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            try:
                from pkgtool import apt_repo
                d = apt_repo.info(name)
                if not d:
                    self._send(404, json.dumps({"error": "仓库中无此包"}, ensure_ascii=False))
                else:
                    self._send(200, json.dumps(d, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        elif self.path.startswith("/api/apt/dl"):
            jid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            job = DL_JOBS.get(jid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(job, ensure_ascii=False))
        elif self.path.startswith("/api/flatpak/job"):
            jid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            job = FLATPAK_JOBS.get(jid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"status": job["status"], "exit_code": job.get("exit_code"),
                     "output": job["output"][-200:]}, ensure_ascii=False))
        elif self.path.startswith("/api/apt/job"):
            jid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            job = APT_JOBS.get(jid)
            if not job:
                self._send(404, json.dumps({"error": "任务不存在"}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"status": job["status"], "exit_code": job.get("exit_code"),
                     "output": job["output"][-200:]}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def do_POST(self):
        if self.path == "/api/refresh":
            threading.Thread(target=collect, daemon=True).start()
            self._send(200, json.dumps({"started": True}))
        elif self.path in ("/api/remove/preview", "/api/remove/execute"):
            body = self._read_json()
            row = STATE["by_key"].get((body.get("type"), body.get("name")))
            if not row or row["extra"].get("class") != "app":
                cls = row["extra"].get("class") if row else "?"
                self._send(403, json.dumps(
                    {"error": BLOCK_REASONS.get(cls, "未知包，拒绝操作"), "class": cls},
                    ensure_ascii=False))
                return
            if self.path == "/api/remove/preview":
                res = preview_remove(row)  # dry-run + 残留扫描（无需 root）
                self._send(200, json.dumps(res, ensure_ascii=False))
            else:
                res = execute_remove(row, bool(body.get("deep", True)),
                                     bool(body.get("autoremove", True)))
                self._send(200 if res["ok"] else 500,
                           json.dumps(res, ensure_ascii=False))
        elif self.path == "/api/apt/download":
            body = self._read_json()
            name, version = body.get("name", ""), body.get("version", "")
            if not name or not version:
                self._send(400, json.dumps({"error": "缺少 name/version"}, ensure_ascii=False))
                return
            jid = uuid.uuid4().hex[:8]
            DL_JOBS[jid] = {"status": "running", "file": None, "error": None}
            threading.Thread(target=_apt_download, args=(jid, name, version), daemon=True).start()
            self._send(200, json.dumps({"id": jid}))
        elif self.path == "/api/apt/upgrade":
            body = self._read_json()
            name, version = body.get("name", ""), body.get("version", "")
            password = body.get("password", "")
            if not name or not version:
                self._send(400, json.dumps({"error": "缺少 name/version"}, ensure_ascii=False))
                return
            cmd = f"sudo apt-get install -y {name}={version}"
            if not password and _sudo_nopass():  # 免密环境直接同步执行
                p = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=900)
                out = (p.stdout or "") + (p.stderr or "")
                self._send(200 if p.returncode == 0 else 500,
                           json.dumps({"ok": p.returncode == 0, "output": out[-2000:]}, ensure_ascii=False))
                return
            if not password:
                self._send(200, json.dumps(
                    {"need_root": True, "command": cmd}, ensure_ascii=False))
                return
            jid = uuid.uuid4().hex[:8]
            APT_JOBS[jid] = {"status": "running", "output": [], "exit_code": None,
                             "cancelled": False, "proc": None}
            threading.Thread(target=_run_apt_upgrade,
                             args=(jid, name, version, password), daemon=True).start()
            self._send(200, json.dumps({"id": jid}))
        elif self.path == "/api/flatpak/update":
            body = self._read_json()
            ids = body.get("ids", [])
            password = body.get("password", "")
            use_sudo = bool(body.get("use_sudo"))
            if not isinstance(ids, list) or not ids or len(ids) > 50:
                self._send(400, json.dumps({"error": "ids 非法"}, ensure_ascii=False))
                return
            bad = [i for i in ids if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z0-9\-]+$", str(i))]
            if bad:
                self._send(400, json.dumps({"error": f"app_id 非法: {bad[:3]}"}, ensure_ascii=False))
                return
            if use_sudo and not password:
                self._send(200, json.dumps(
                    {"need_root": True, "command": "sudo flatpak update -y " + " ".join(ids)}, ensure_ascii=False))
                return
            jid = uuid.uuid4().hex[:8]
            FLATPAK_JOBS[jid] = {"status": "running", "output": [], "exit_code": None,
                                 "cancelled": False, "proc": None}
            threading.Thread(target=_run_flatpak_update,
                             args=(jid, ids, use_sudo, password), daemon=True).start()
            self._send(200, json.dumps({"id": jid}))
        elif self.path == "/api/flatpak/cancel":
            body = self._read_json()
            job = FLATPAK_JOBS.get(body.get("id", ""))
            ok = False
            if job and job["status"] == "running" and job.get("proc"):
                try:
                    os.killpg(os.getpgid(job["proc"].pid), signal.SIGTERM)
                    job["cancelled"] = True
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, json.dumps({"ok": ok}))
        elif self.path == "/api/apt/cancel":
            body = self._read_json()
            job = APT_JOBS.get(body.get("id", ""))
            ok = False
            if job and job["status"] == "running" and job.get("proc"):
                try:
                    os.killpg(os.getpgid(job["proc"].pid), signal.SIGTERM)
                    job["cancelled"] = True
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, json.dumps({"ok": ok}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>软件盘点 · pkgtool</title>
<style>
:root{--bg:#0f1420;--panel:#171e2e;--panel2:#1d2639;--text:#e6ebf5;--muted:#8b96ad;
--deb:#34d399;--snap:#60a5fa;--flatpak:#c084fc;--local:#fbbf24;--accent:#3b82f6;--line:#26314a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;padding:20px 28px 60px}
h1{font-size:20px;font-weight:700}
.top{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.top .grow{flex:1}
.pill{font-size:12px;padding:3px 10px;border-radius:99px;background:var(--panel);border:1px solid var(--line);color:var(--muted)}
.pill.busy{color:var(--local);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.45}}
button{background:var(--accent);border:0;color:#fff;padding:7px 16px;border-radius:8px;cursor:pointer;font-size:13px}
button:hover{filter:brightness(1.15)}
button.ghost{background:var(--panel);border:1px solid var(--line);color:var(--text)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:18px}
section h2{font-size:15px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
section h2 .cnt{color:var(--muted);font-weight:400;font-size:12px}
.bar-row{display:grid;grid-template-columns:260px 1fr 70px;gap:10px;align-items:center;margin-bottom:7px;font-size:13px}
.bar-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}
.bar-track{background:var(--panel2);border-radius:6px;height:14px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#3b82f6,#60a5fa);min-width:2px}
.bar-fill.local{background:linear-gradient(90deg,#d97706,#fbbf24)}
.env-grid{display:flex;flex-wrap:wrap;gap:8px}
.env-chip{background:var(--panel2);border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:12px}
.env-chip b{color:#a78bfa}
.env-chip.empty-env{border-color:#5a3a1a;color:#d8a06a}
.env-chip:not(.empty-env){cursor:pointer}
.env-chip.on{border-color:#a78bfa;background:#221c38}
#vbanner{position:fixed;top:0;left:0;right:0;z-index:99;background:#7c2d12;color:#fed7aa;padding:10px 16px;font-size:13px;text-align:center;border-bottom:1px solid #f59e0b}
.bar-row.clickable{cursor:pointer}
.bar-row.clickable:hover .bar-label{color:#a78bfa}
.bar-row.sub{padding-left:20px;opacity:.85}
.bar-fill.pipfill{background:linear-gradient(90deg,#4c1d95,#a78bfa)}
.apt-row{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:pointer}
.apt-row:hover{border-color:#a78bfa}
.apt-row .nm{font-weight:600}
.badge.up{background:#332708;color:#fbbf24;cursor:help}
.bar-num{text-align:right;color:var(--muted)}
.badge{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;font-weight:600}
.badge.deb{background:#0d2b21;color:var(--deb)}.badge.snap{background:#12233d;color:var(--snap)}
.badge.flatpak,.badge.flatpak-runtime{background:#271a3d;color:var(--flatpak)}
.badge.app{background:#0d2b21;color:var(--deb)}.badge.library{background:#202940;color:#9aa7c4}
.badge.system{background:#2b2318;color:#d8a06a}.badge.base{background:#3a1518;color:#f87171}
.badge.file{background:#1e3a5f;color:#7dd3fc}
button.danger{background:transparent;border:1px solid #7f1d1d;color:#f87171;padding:3px 10px;font-size:12px}
button.danger:hover{background:#3a1518;filter:none}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:10}
.modal.show{display:flex}
.modal-box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;width:540px;max-width:92vw;max-height:80vh;overflow:auto}
pre.mono{white-space:pre-wrap;background:var(--panel2);padding:10px;border-radius:8px;font-size:12px}
.controls{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
input[type=search]{flex:1;min-width:220px;background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px}
.chip{background:var(--panel2);border:1px solid var(--line);color:var(--muted);padding:6px 14px;border-radius:99px;cursor:pointer;font-size:13px}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
label.tog{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:0;background:var(--panel);text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #1e2941;vertical-align:top}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--panel2)}
.tbl-wrap{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:8px}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#aeb9d4;word-break:break-all}
.dim{color:var(--muted)}
#drawer{position:fixed;top:0;right:-560px;width:540px;max-width:92vw;height:100vh;background:var(--panel);border-left:1px solid var(--line);transition:right .25s;overflow-y:auto;padding:22px;z-index:9}
#drawer.open{right:0}
#drawer h3{font-size:17px;margin-bottom:4px;word-break:break-all}
.kv{margin-top:14px}
.kv .k{color:var(--muted);font-size:12px;margin-top:10px}
.kv .v{word-break:break-all;margin-top:2px}
#mask{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;z-index:8}
#mask.show{display:block}
.empty{color:var(--muted);text-align:center;padding:30px 0}
</style>
</head>
<body>
<div class="top">
  <h1>软件盘点 · pkgtool</h1>
  <span id="status" class="pill">连接中…</span>
  <span id="pvbadge" class="pill dim" title="页面 JS 版本（应与右上角数据一致；不对请 Ctrl+Shift+R 强刷）"></span>
  <div class="grow"></div>
  <button class="ghost" onclick="refreshData()">↻ 重新采集</button>
</div>
<section><h2>来源分布</h2><div id="bars"><div class="empty">—</div></div></section>
<section><h2>Python 环境 <span class="cnt">pip 嗅探覆盖（空壳=没装 python）</span></h2><div id="envs" class="env-grid"><div class="empty">—</div></div></section>
<section>
  <h2>APT 仓库 <span class="cnt">搜索 · 下载 · 选版本 · 升级（主表格中可升级的包标 ↑）</span></h2>
  <div style="display:flex;gap:8px;margin-bottom:10px">
    <input type="search" id="aptq" placeholder="搜索包名或描述，如 tmux / ffmpeg / video editor" style="flex:1" onkeydown="if(event.key==='Enter')aptSearch()">
    <button onclick="aptSearch()">搜索</button>
  </div>
  <div id="aptres"><div class="empty">覆盖所有已配置的 apt 源（Ubuntu 官方/安全更新/PPA/第三方），输入关键词搜索</div></div>
</section>
<section>
  <h2>全部包 <span class="cnt" id="tblcnt"></span> <span class="cnt">默认隐藏系统预装与自动依赖</span></h2>
  <div class="controls">
    <input type="search" id="q" placeholder="搜索包名、版本、来源、路径…（如 wechat / cuda / flathub）">
    <span class="chip on" data-t="all">全部</span>
    <span class="chip" data-t="deb">deb</span>
    <span class="chip" data-t="snap">snap</span>
    <span class="chip" data-t="flatpak">flatpak</span>
    <span class="chip" data-t="appimage">appimage</span>
    <span class="chip" data-t="pip">pip</span>
    <label class="tog"><input type="checkbox" id="onlylocal"> 仅本地自装</label>
    <label class="tog" title="默认隐藏：镜像预装 + apt 自动标记的依赖/底层库；勾选后全部显示"><input type="checkbox" id="showsys"> 显示系统预装模块（含依赖/底层库）</label>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr>
      <th data-k="pkg_type">类型</th><th data-k="name">名称</th><th data-k="version">版本</th>
      <th>来源</th><th data-k="first_install">首次安装</th><th>可执行</th><th>操作</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table></div>
</section>
<div id="mask" onclick="closeDrawer()"></div>
<div id="drawer"></div>
<div class="modal" id="modal"><div class="modal-box">
  <h3 id="modalTitle" style="font-size:16px"></h3>
  <div id="modalBody" style="margin-top:12px;font-size:13px"></div>
</div></div>
<script>
const PAGE_VERSION=19;
let DATA=null, state={q:'',type:'all',onlyLocal:false,showSys:false,env:null,open:{},vbannerClosed:false,sortK:'pkg_type',sortD:1}, CUR=null;
const CLASS_CN={app:'软件',library:'库/依赖',system:'系统组件',base:'基础包',file:'包文件'};
const $=s=>document.querySelector(s);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function isLocal(r){
  if(r.pkg_type==='deb') return r.origin.startsWith('local');
  if(r.pkg_type==='flatpak-runtime') return false;
  if(r.pkg_type==='appimage') return !(r.extra&&r.extra.loose); // 已安置的便携应用算自装；散落文件不算
  return !r.origin.includes('flathub') && !r.origin.includes('snap store');
}
function isSystem(r){ // 系统/底层库：deb 镜像预装 + apt 自动标记的依赖（libc6、libpng…）
  if(r.pkg_type!=='deb') return false;
  const e=r.extra||{};
  if(String(e.channel||'').startsWith('preinstalled')) return true;
  return e.apt_mark==='auto' || (e.apt_mark==='?' && e.ext_states==='auto');
}
function originBucket(r){
  if(r.extra&&r.extra.loose) return r.pkg_type+': 磁盘包文件(散落)';
  if(r.pkg_type==='deb'){
    if(r.origin.startsWith('local')) return 'deb: 本地(不在任何源)';
    if(r.origin.startsWith('repo-')) return 'deb: repo版本已更新/源可能移除';
    if(r.origin.includes('Ubuntu')) return 'deb: Ubuntu官方源(含镜像)';
    return 'deb: 第三方源 '+r.origin.split(' | ')[0];
  }
  return r.pkg_type+': '+r.origin;
}
function visible(){
  const q=state.q.toLowerCase();
  return DATA.rows.filter(r=>{
    if(state.type!=='all' && !(state.type==='flatpak'? r.pkg_type.startsWith('flatpak') : r.pkg_type===state.type)) return false;
    if(!state.showSys && isSystem(r)) return false;
    if(state.env && !(r.pkg_type==='pip'&&r.extra&&r.extra.env===state.env)) return false; // 环境过滤
    if(state.onlyLocal && !isLocal(r)) return false;
    if(q && !(r.name+' '+r.version+' '+r.origin+' '+(r.install_path||'')).toLowerCase().includes(q)) return false;
    return true;
  });
}
function sortRows(rows){const k=state.sortK,d=state.sortD;return rows.slice().sort((a,b)=>String(a[k]??'').localeCompare(String(b[k]??''))*d)}
function renderAll(){
  const rows=visible();
  const GROUP={pip:'pip（全部环境）',deb:'deb（apt/dpkg）',snap:'snap（snap store）'}; // 分组折叠：合并成一条，点击展开明细
  const buckets={};const subs={};rows.forEach(r=>{
    if(GROUP[r.pkg_type]){buckets[r.pkg_type]=(buckets[r.pkg_type]||0)+1;
      const d=r.pkg_type==='pip'?((r.extra&&r.extra.env)||'?')
            :r.pkg_type==='snap'?r.name // snap 展开=各包名
            :originBucket(r).replace(/^deb: /,'');
      (subs[r.pkg_type]=subs[r.pkg_type]||{})[d]=((subs[r.pkg_type][d])||0)+1;}
    else{const b=originBucket(r);buckets[b]=(buckets[b]||0)+1}});
  const max=Math.max(1,...Object.values(buckets));
  const out=[];
  for(const [k,v] of Object.entries(buckets).sort((a,b)=>b[1]-a[1])){
    if(GROUP[k]){
      const open=!!state.open[k];
      out.push(`<div class="bar-row clickable" onclick="toggleGroup('${k}')">`+
        `<span class="bar-label">${open?'▾':'▸'} ${GROUP[k]}<span class="dim"> 点击${open?'收起':'展开'}</span></span>`+
        `<div class="bar-track"><div class="bar-fill ${k==='pip'?'pipfill':''}" style="width:${(v/max*100).toFixed(1)}%"></div></div>`+
        `<span class="bar-num">${v}</span></div>`);
      if(open) out.push(...Object.entries(subs[k]).sort((a,b)=>b[1]-a[1]).map(([k2,v2])=>
        `<div class="bar-row sub"><span class="bar-label" title="${esc(k2)}">${esc(k2)}</span>`+
        `<div class="bar-track"><div class="bar-fill ${k==='pip'?'pipfill':(k2.includes('本地')?' local':'')}" style="width:${(v2/max*100).toFixed(1)}%"></div></div>`+
        `<span class="bar-num">${v2}</span></div>`));
    } else {
      out.push(`<div class="bar-row"><span class="bar-label" title="${esc(k)}">${esc(k)}</span>`+
        `<div class="bar-track"><div class="bar-fill${k.includes('本地')?' local':''}" style="width:${(v/max*100).toFixed(1)}%"></div></div>`+
        `<span class="bar-num">${v}</span></div>`);
    }
  }
  $('#bars').innerHTML=out.join('')||'<div class="empty">—</div>';
  const envs=(DATA.meta&&DATA.meta.py_envs)||[];
  $('#envs').innerHTML=envs.map(e=>
    `<span class="env-chip${e.empty?' empty-env':''}${state.env===e.label?' on':''}" title="${esc(e.label)} — 点击过滤表格" onclick="toggleEnv('${String(e.label).replace(/'/g,"\\'")}')">${esc(e.label)} <b>${e.packages}</b>${e.empty?' · 空壳(无python)':''}</span>`).join('')||'<div class="empty">—</div>';
  renderTable(rows);
  const m=DATA.meta;
  $('#status').textContent=`实时 · ${DATA.rows.length} 个包 · 更新于 ${m.updated} · 采集${m.elapsed}s`;
  $('#status').classList.remove('busy');
}
function renderTable(rows){
  const s=sortRows(rows);$('#tblcnt').textContent=`显示 ${s.length} / ${DATA.rows.length}`;
  $('#tbody').innerHTML=s.map(r=>{
    const ex=(r.executables||[]).length,cls=(r.extra&&r.extra.class)||'';
    const arg=JSON.stringify(r).replace(/'/g,"\\'");
    const loose=r.extra&&r.extra.loose;
    const upg=r.pkg_type==='deb'?(DATA.meta.apt_upgradable||[]).find(u=>u.name===r.name):null;
    const fupg=r.pkg_type.startsWith('flatpak')?(DATA.meta.flatpak_updates||[]).find(u=>u.app_id===r.name):null;
    const btn=loose
      ?`<span class="dim" title="${esc(loose)}">文件·${esc((r.extra.state||'').slice(0,8))}</span>`
      :(cls==='app'
        ?`<button class="danger" onclick='event.stopPropagation();openRemove(${arg})'>卸载</button>`
        :`<span class="dim" title="${esc((r.extra&&r.extra.class_reason)||'')}">不可删</span>`);
    return `<tr class="row" onclick='openDrawer(${arg})'>`+
      `<td><span class="badge ${r.pkg_type}">${esc(r.pkg_type)}</span>${isLocal(r)?' <span class="badge local">本地</span>':''} `+
      `<span class="badge ${cls||'system'}" title="${esc((r.extra&&r.extra.class_reason)||'')}">${esc(CLASS_CN[cls]||cls)}</span></td>`+
      `<td>${esc(r.name)}</td><td class="mono">${esc(r.version)}${upg?` <span class="badge up" title="apt 可升级 → ${esc(upg.candidate)}（点行查看详情）">↑</span>`:''}${fupg?` <span class="badge up" title="flathub 有新版本：flatpak update ${esc(r.name)}">↑</span>`:''}</td><td class="dim">${esc(r.origin)}</td>`+
      `<td class="dim">${esc((r.first_install||'').slice(0,10))||'—'}</td><td class="dim">${ex?ex+' 个':'—'}</td><td>${btn}</td></tr>`;
  }).join('')||'<tr><td colspan=7><div class="empty">无匹配结果</div></td></tr>';
}
// 更新通道：按安装来源分流升级方式（apt→apt升级；其他→软件自带推送）
function selfUpdHint(name){
  const n=name.toLowerCase();
  if(n.includes('wechat')||n.includes('weixin')) return '微信内置更新推送（应用内自动检查）；也可官网下载新 .deb 覆盖安装';
  if(n.includes('adspower')) return 'AdsPower 客户端内置更新（设置页检查更新）';
  if(n.includes('clash-verge')||n.includes('clash verge')) return 'Clash Verge 应用内自动更新，或 GitHub Releases 下载新版';
  if(n.includes('cc-switch')) return 'GitHub Releases 下载新版 cc-switch 覆盖安装';
  if(n.includes('motrix')) return 'Motrix 应用内有更新提示，或官网/GitHub 下载新版';
  if(n.includes('anaconda-desktop')||n.includes('anaconda')) return 'Anaconda Desktop 应用内检查更新；或 conda update -n base anaconda';
  if(n.includes('cuda-repo')) return 'NVIDIA 本地仓库包：CUDA 升级时从官网重新下载 .deb 安装即可';
  if(n.includes('chrome')) return 'Chrome 自带更新器，自动推送更新';
  if(n==='code') return 'VS Code：Help→Check for Updates（若走 Microsoft apt 源则可直接 apt 升级）';
  return '通常应用内有“检查更新”；或到官网下载新 .deb/AppImage 覆盖安装';
}
function updSection(r){
  const upg=(DATA.meta.apt_upgradable||[]).find(u=>u.name===r.name);
  let html='';
  if(r.pkg_type==='deb'&&!(r.extra&&r.extra.loose)){
    if((r.extra.channel||'')==='apt'){
      html=upg
        ?`<div class="k">更新通道</div><div class="v" style="color:#fbbf24">apt 源安装 · 可升级 ${esc(upg.installed)} → ${esc(upg.candidate)}</div><div style="margin:8px 0"><button onclick='openApt({name:${JSON.stringify(r.name)}})'>🔧 apt 升级（可中止）</button></div>`
        :`<div class="k">更新通道</div><div class="v" style="color:#4ade80">apt 源安装 · 仓库内已是最新</div>`;
    } else if((r.extra.channel||'')==='apt-local'){
      html=`<div class="k">更新通道</div><div class="v" style="color:#fbbf24">apt install ./xxx.deb 安装（当前无源可更新）→ 走软件自带推送</div>`+
        `<div class="dim" style="margin:4px 0">${esc(selfUpdHint(r.name))}</div>`;
    } else {
      html=`<div class="k">更新通道</div><div class="v" style="color:#fbbf24">dpkg -i 手动安装（本地 .deb）→ 走软件自带推送</div>`+
        `<div class="dim" style="margin:4px 0">${esc(selfUpdHint(r.name))}</div>`;
    }
  } else if(r.pkg_type==='snap'){
    html=`<div class="k">更新通道</div><div class="v">snap store 推送（snapd 每天自动刷新）</div>`+
      `<div class="dim" style="margin:4px 0">手动强制： <span class="mono">sudo snap refresh ${esc(r.name)}</span></div>`;
  } else if(r.pkg_type.startsWith('flatpak')){
    const fu=(DATA.meta.flatpak_updates||[]).find(u=>u.app_id===r.name);
    html=fu
      ?`<div class="k">更新通道</div><div class="v" style="color:#fbbf24">flathub 有新版本（远端 ${esc(fu.name)}）· 已装 ${esc(r.version)}</div>`+
        `<div style="margin:8px 0"><button onclick="openFlatpakUpgById(this)" data-id="${esc(r.name)}">🔧 flatpak 升级（可中止）</button> `+
        `<button class="ghost" data-cmd="flatpak update ${esc(r.name)}" onclick="copyCmd(this)">📋 复制命令</button></div>`
      :`<div class="k">更新通道</div><div class="v">flathub 推送 · <span class="mono">flatpak update --user ${esc(r.name)}</span>（未检测到新版）</div>`;
  } else if(r.pkg_type==='pip'){
    const env=r.extra&&r.extra.env;
    html=`<div class="k">更新通道</div><div class="v">Python 环境内更新： <span class="mono">pip install -U ${esc(r.name)}</span>${env?` <span class="dim">(env: ${esc(env)})</span>`:''}</div>`;
  } else if(r.pkg_type==='appimage'||(r.extra&&r.extra.loose)){
    html=`<div class="k">更新通道</div><div class="v">便携应用/散落文件 —— 无系统级更新通道</div>`+
      `<div class="dim" style="margin:4px 0">${esc(selfUpdHint(r.name))}</div>`;
  }
  return html;
}
// 内联事件安全模式：数据放 data-*（经 esc 转义），onclick 只调无参函数——避免 JSON.stringify 的双引号截断属性
function openFlatpakUpgById(b){openFlatpakUpg(b.dataset.id)}
function openAptByName(b){openApt({name:b.dataset.name})}
function copyCmd(b){navigator.clipboard.writeText(b.dataset.cmd).then(()=>{b.textContent='已复制 ✓';setTimeout(()=>{b.textContent=b.dataset.label||'📋 复制命令'},2000)})}
function openDrawer(r){
  const kv=(k,v)=>v?`<div class="k">${esc(k)}</div><div class="v mono">${esc(v)}</div>`:'';
  $('#drawer').innerHTML=`<span class="badge ${r.pkg_type}">${esc(r.pkg_type)}</span> <h3>${esc(r.name)}</h3>`+
    kv('版本',r.version)+kv('来源',r.origin)+kv('首次安装',r.first_install)+kv('安装路径',r.install_path)+
    `<div class="k">可执行文件 (${(r.executables||[]).length})</div><div class="v mono">${esc((r.executables||[]).join('\n')||'—')}</div>`+
    updSection(r)+
    Object.entries(r.extra||{}).map(([k,v])=>kv(k,v)).join('')+`
    <div style="margin-top:20px"><button class="ghost" onclick="closeDrawer()">关闭</button></div>`;
  $('#drawer').classList.add('open');$('#mask').classList.add('show');
}
function closeDrawer(){$('#drawer').classList.remove('open');$('#mask').classList.remove('show')}
async function openRemove(r){
  CUR={row:r};
  $('#modalTitle').textContent='卸载 '+r.name+' ?';
  $('#modalBody').innerHTML='<div class="dim">正在模拟卸载（dry-run，不会真的删）…</div>';
  $('#modal').classList.add('show');
  const p=await fetch('/api/remove/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:r.pkg_type,name:r.name})});
  const d=await p.json();
  if(!p.ok){$('#modalBody').innerHTML=`<div style="color:#f87171">${esc(d.error)}</div>`;return}
  CUR.preview=d;
  const resHtml=(d.residues&&d.residues.length)
    ? d.residues.map(x=>`<div>• ${esc(x.path)} <span class="dim">${x.size_mb} MB</span></div>`).join('')
    : '<div class="dim">未检测到配置/缓存/数据残留</div>';
  const deepLabel=r.pkg_type==='snap'?'彻底清理（含保存数据 --purge）'
    :r.pkg_type==='flatpak'?'彻底清理（含应用数据 --delete-data）':'彻底清理（删除配置/缓存/数据残留）';
  $('#modalBody').innerHTML=
    `<div>类别：<span class="badge app">软件</span> <span class="dim">${esc((r.extra&&r.extra.class_reason)||'')}</span></div>`+
    `<div class="k" style="margin-top:12px;color:var(--muted);font-size:12px">将移除：</div>`+
    `<pre class="mono">${esc(d.will_remove.join('\n')||r.name)}</pre>`+
    `<div class="k" style="margin-top:12px;color:var(--muted);font-size:12px">将一并删除的残留（共 ${d.freed_mb} MB）：</div>`+
    `<div style="font-size:12px;line-height:1.7">${resHtml}</div>`+
    `<label class="tog" style="margin-top:8px"><input type="checkbox" id="deep" checked> ${deepLabel}</label>`+
    (r.pkg_type==='deb'?`<label class="tog" style="margin-top:6px;display:block"><input type="checkbox" id="ar" checked> 同时清理孤儿依赖（--autoremove）</label>`:'')+`
    <div class="k" style="margin-top:12px;color:var(--muted);font-size:12px">等效命令（密码框超时/取消时手动执行）：</div>`+
    `<pre class="mono">${esc(d.command)}</pre>`+
    `<div style="display:flex;gap:10px;margin-top:16px"><button class="ghost" onclick="closeModal()">取消</button>`+
    `<button class="danger" style="padding:7px 16px;font-size:13px" onclick="doRemove()">确认卸载（将弹出系统密码框）</button></div>`;
}
async function doRemove(){
  const ar=document.getElementById('ar')?document.getElementById('ar').checked:true;
  const deep=document.getElementById('deep')?document.getElementById('deep').checked:true;
  $('#modalBody').innerHTML='<div class="dim">正在卸载… 请在弹出的系统对话框中输入密码（最长等待180秒）</div>';
  const p=await fetch('/api/remove/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:CUR.row.pkg_type,name:CUR.row.name,autoremove:ar,deep})});
  const d=await p.json();
  $('#modalBody').innerHTML=d.ok
    ?`<div style="color:var(--deb);font-weight:600">✓ 卸载完成${(d.residues_removed&&d.residues_removed.length)?'，已删除 '+d.residues_removed.length+' 处残留':''}</div><pre class="mono">${esc(d.output)}</pre>`+
     `<button onclick="closeModal();refreshData()">好的，刷新数据</button>`
    :`<div style="color:var(--local)">未删除成功（可能取消了密码框或超时），请手动执行：</div><pre class="mono">${esc(d.command)}</pre>`+
     `<div style="display:flex;gap:10px;margin-top:10px"><button class="ghost" onclick="navigator.clipboard.writeText(CUR.preview?CUR.preview.command:'')">复制命令</button><button onclick="closeModal()">关闭</button></div>`;
}
function closeModal(){$('#modal').classList.remove('show')}
async function poll(){
  try{
    const r=await fetch('/api/data');
    if(r.status===202){ // 服务器正在重新采集：已有数据就静默等待，保留旧画面
      if(!DATA){const p=$('#status');p.textContent='采集中…（首次约10-30秒）';p.classList.add('busy');}
      setTimeout(poll,1500);return;
    }
    if(!r.ok){const e=await r.json();$('#status').textContent='错误: '+e.error;return}
    const nd=await r.json();
    if(DATA && nd.meta.updated===DATA.meta.updated) return; // 数据没变→不重渲染，不打断搜索/详情
    DATA=nd;renderAll();checkVersion();
    if(nd.meta.version!==PAGE_VERSION)$('#pvbadge').style.color='#fbbf24'; // 版本不一致→徽章变黄提醒
  }catch(e){setTimeout(poll,2000)}
}
async function refreshData(){const p=$('#status');p.textContent='采集中…';p.classList.add('busy');await fetch('/api/refresh',{method:'POST'});poll()}
function toggleEnv(l){state.env=(state.env===l?null:l);renderAll()}
function toggleGroup(t){state.open[t]=!state.open[t];renderAll()}
function checkVersion(){ // 页面代码比服务端旧 → 顶部横幅提醒强制刷新（可 ✕ 关闭，本轮会话不再弹）
  if(!DATA||!DATA.meta.version||$('#vbanner')||state.vbannerClosed) return;
  if(DATA.meta.version===PAGE_VERSION) return;
  const b=document.createElement('div');b.id='vbanner';
  b.innerHTML=`⚠️ 页面代码已更新（当前 v${DATA.meta.version}，本页面 v${PAGE_VERSION}），你看到的可能不是最新版 —— <button style="margin-left:8px" onclick="location.reload()">立即刷新</button>`+
    `<button style="position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#fed7aa;font-size:15px;cursor:pointer" title="关闭提醒（本轮会话不再弹）" onclick="dismissBanner()">✕</button>`;
  document.body.prepend(b);
}
function dismissBanner(){state.vbannerClosed=true;const b=$('#vbanner');if(b)b.remove()}
// ---------- APT 仓库：搜索 / 详情 / 下载 / 升级 ----------
async function aptSearch(){
  const q=$('#aptq').value.trim();
  if(!q){$('#aptres').innerHTML='<div class="empty">请输入关键词</div>';return}
  $('#aptres').innerHTML='<div class="dim">搜索中…</div>';
  try{
    const r=await fetch('/api/apt/search?q='+encodeURIComponent(q)+'&limit=40');
    const list=await r.json();
    if(!list.length){$('#aptres').innerHTML='<div class="empty">未找到相关包</div>';return}
    $('#aptres').innerHTML=list.map(x=>
      `<div class="apt-row" onclick='openApt(${JSON.stringify({name:x.name,desc:x.desc}).replace(/'/g,"\\'")})'>`+
      `<span class="nm">${esc(x.name)}</span> <span class="dim mono">${esc(x.version)}</span>`+
      `<div class="mt" style="color:var(--muted);font-size:12px;margin-top:3px">作者 ${esc(x.maintainer)} · ${x.size_kb} KB · 源 ${esc(x.source||'?')} · 仓库更新 ${esc(x.date||'?')}</div>`+
      `<div class="dim" style="margin-top:2px">${esc(x.desc||'')}</div></div>`).join('');
  }catch(e){$('#aptres').innerHTML='<div class="empty">搜索失败: '+esc(e)+'</div>'}
}
async function openApt(x){
  $('#modalTitle').textContent='APT · '+x.name;
  $('#modalBody').innerHTML='<div class="dim">加载版本列表…</div>';
  $('#modal').classList.add('show');
  try{
    const r=await fetch('/api/apt/info?name='+encodeURIComponent(x.name));
    const d=await r.json();
    if(d.error){$('#modalBody').innerHTML='<div class="empty">'+esc(d.error)+'</div>';return}
    window._APT=d;
    const upg=(DATA.meta.apt_upgradable||[]).find(u=>u.name===d.name);
    const latestV=d.versions.find(v=>v.version===d.latest)||d.versions[0];
    $('#modalBody').innerHTML=
      `<div style="margin-bottom:8px">${esc(latestV.desc||x.desc||'')}</div>`+
      `<div style="margin-bottom:10px">${upg?`已安装 <b class="mono">${esc(upg.installed)}</b> · <span style="color:#fbbf24">可升级 → ${esc(upg.candidate)}</span>`:'当前未安装'}</div>`+
      `<label class="dim">选择版本（共 ${d.versions.length} 个）</label><select id="aptver" style="width:100%;margin:6px 0">`+
      d.versions.map(v=>`<option value="${esc(v.version)}"${v.version===d.latest?' selected':''}>${esc(v.version)} — ${esc([v.suite,v.component].filter(Boolean).join('/'))} · ${esc(v.maintainer)} · ${v.size_kb}KB</option>`).join('')+
      `</select><div id="aptmsg" class="dim" style="margin:8px 0"></div>`+
      `<button onclick="showUpgPanel()">🔧 用 apt 安装/升级此版本（需 sudo 密码，可中止）</button> `+
      `<button class="ghost" onclick="aptDl()" title="只下载 .deb 文件不安装、无需 root；之后需手动 sudo dpkg -i">⬇ 仅下载 .deb（不安装）</button>`;
  }catch(e){$('#modalBody').innerHTML='<div class="empty">加载失败: '+esc(e)+'</div>'}
}
async function aptDl(){
  const v=$('#aptver').value,msg=$('#aptmsg');
  msg.textContent='下载中…（大文件需要几分钟）';
  try{
    const r=await fetch('/api/apt/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:window._APT.name,version:v})});
    const j=await r.json();pollDl(j.id,msg);
  }catch(e){msg.textContent='失败: '+e}
}
async function pollDl(id,msg,n){
  n=n||0;
  if(n>240){msg.textContent='轮询超时，下载可能仍在进行';return}
  await new Promise(s=>setTimeout(s,2000));
  try{
    const r=await fetch('/api/apt/dl?id='+id);const j=await r.json();
    if(j.status==='done'){msg.innerHTML=`✅ 已保存到 <span class="mono">${esc(j.file)}</span>`;return}
    if(j.status==='error'){msg.textContent='❌ '+j.error;return}
    pollDl(id,msg,n+1);
  }catch(e){pollDl(id,msg,n+1)}
}
// apt 升级：UI 输密码 → 后台 sudo -S apt-get install → 实时回显 + ✕ 中止
function showUpgPanel(){
  const d=window._APT,v=$('#aptver')?$('#aptver').value:(window._UPGVER||d.latest);
  window._UPGVER=v; // 面板会替换弹窗内容，版本选择框销毁前先把值存下来
  window._UPGCMD=`sudo apt-get install -y ${d.name}=${v}`;
  $('#modalBody').innerHTML=
    `<div style="margin-bottom:8px">目标版本 <b class="mono">${esc(v)}</b></div>`+
    `<label class="dim">sudo 密码（仅本次升级使用，走 stdin 传给 sudo，不存储不记日志）</label>`+
    `<input id="aptpw" type="password" placeholder="输入当前用户的 sudo 密码" style="width:100%;margin:6px 0" onkeydown="if(event.key==='Enter')startAptUpg()">`+
    `<button onclick="startAptUpg()">🔧 开始 apt 升级</button> `+
    `<button class="ghost" data-cmd="${esc(window._UPGCMD)}" data-label="📋 复制命令自己到终端跑" onclick="copyCmd(this)">📋 复制命令自己到终端跑</button>`;
}
async function startAptUpg(){
  try{
    const d=window._APT,v=window._UPGVER,pw=$('#aptpw')?$('#aptpw').value:'';
    if(!d||!v){$('#modalBody').innerHTML='<div style="color:#f87171">缺少包信息，请重新打开详情</div>';return}
    $('#modalBody').innerHTML='<div class="dim">正在启动升级…</div>';
    const r=await fetch('/api/apt/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:d.name,version:v,password:pw})});
    const j=await r.json();
    if(j.need_root){$('#modalBody').innerHTML='<div style="color:#f87171">未提供 sudo 密码（或免密检测不可用）。输入密码重试，或点“复制命令”自己到终端执行。</div><button class="ghost" onclick="showUpgPanel()">← 返回</button>';return}
    window._APT_JOB=j.id;renderJob('apt',j.id,0);
  }catch(e){$('#modalBody').innerHTML='<div style="color:#f87171">启动失败: '+esc(e)+'</div>'}
}
// 通用升级任务渲染：kind='apt'|'flatpak'（实时回显 + ✕ 中止 + 四种终态）
async function renderJob(kind,id,n){
  n=n||0;
  const guard=kind==='apt'?window._APT_JOB:window._FPJOB;
  if(guard!==id)return; // 已切走
  try{
    const r=await fetch('/api/'+kind+'/job?id='+id);const j=await r.json();
    const out=(j.output||[]).join('\n');
    const backBtn=kind==='apt'
      ?`<button class="ghost" data-name="${esc(window._APT.name)}" onclick="openAptByName(this)">← 返回选版本</button>`
      :`<button class="ghost" onclick="closeModal();refreshData()">完成，刷新数据</button>`;
    let tail='';
    if(j.status==='running') tail=`<div style="margin:8px 0"><button class="danger" onclick="cancelJob('${kind}','${id}')">✕ 中止升级</button> <span class="dim">任务 ${id} · 中止后若锁被占用（dpkg/flatpak），等几秒再重试</span></div>`;
    else if(j.status==='done') tail=`<div style="color:#4ade80;margin:8px 0">✅ 升级完成（exit ${j.exit_code}）</div>${backBtn}`;
    else if(j.status==='cancelled') tail=`<div style="color:#fbbf24;margin:8px 0">⚠️ 升级已中止。若提示锁被占用，等几秒后重试。</div>${backBtn}`;
    else tail=`<div style="color:#f87171;margin:8px 0">❌ 升级失败（exit ${j.exit_code}）—— 看上方输出定位（常见：密码错误 / 网络 / 远端不可达）</div>${backBtn}`;
    $('#modalBody').innerHTML=`<pre class="mono" style="background:#0c0a12;border:1px solid var(--line);padding:10px;border-radius:8px;margin:10px 0;white-space:pre-wrap;max-height:300px;overflow:auto;font-size:12px">${esc(out||'(等待输出…)')}</pre>`+tail;
    if(j.status==='running'&&n<400) setTimeout(()=>renderJob(kind,id,n+1),1500);
  }catch(e){if(n<400)setTimeout(()=>renderJob(kind,id,n+1),2000)}
}
async function cancelJob(kind,id){
  try{await fetch('/api/'+kind+'/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})}catch(e){} // 轮询会继续，状态由服务端判定
}
// flatpak 升级界面：可更新列表 + 真实执行 + 实时回显 + ✕ 中止（系统级安装需 sudo）
function openFlatpakUpg(pre){
  const ups=DATA.meta.flatpak_updates||[];
  if(!ups.length){alert('flathub 未检测到可更新应用');return}
  window._FPSEL=new Set(pre?[pre]:ups.map(u=>u.app_id));
  $('#modalTitle').textContent='📦 flatpak 升级（flathub 推送）';
  $('#modalBody').innerHTML=
    ups.map(u=>{
      const row=(DATA.rows||[]).find(r=>r.pkg_type.startsWith('flatpak')&&r.name===u.app_id);
      const inst=row&&row.extra&&row.extra.installation;
      return `<label style="display:block;margin:8px 0;cursor:pointer"><input type="checkbox" class="fpck" value="${esc(u.app_id)}" ${window._FPSEL.has(u.app_id)?'checked':''}> `+
        `<b>${esc(row&&row.extra.display_name?row.extra.display_name:u.name)}</b> <span class="dim mono">${esc(u.app_id)}</span>`+
        (row?` · 已装 <span class="mono">${esc(row.version)}</span>`:'')+
        `<span class="badge ${inst==='system'?'system':''}" style="margin-left:6px;${inst!=='system'?'background:#0d2b21;color:#4ade80':''}">${inst==='system'?'系统级 · 需 sudo':'用户级'}</span></label>`;
    }).join('')+
    `<label class="dim" style="display:block;margin-top:10px">sudo 密码（仅当勾选含 <b>系统级</b> 安装时需要；全用户级留空即可）</label>`+
    `<input id="fppw" type="password" placeholder="（留空 = 直接以当前用户执行）" style="width:100%;margin:6px 0">`+
    `<div style="margin-top:8px"><button onclick="startFlatpakUpg()">🔧 开始升级（已勾选的）</button> `+
    `<button class="ghost" onclick="closeModal()">取消</button></div>`;
  $('#modal').classList.add('show');
}
async function startFlatpakUpg(){
  const ids=[...document.querySelectorAll('.fpck:checked')].map(c=>c.value);
  if(!ids.length){alert('请至少勾选一个应用');return}
  const needSudo=ids.some(i=>{const row=(DATA.rows||[]).find(r=>r.name===i&&r.pkg_type.startsWith('flatpak'));return row&&row.extra&&row.extra.installation==='system'});
  const pw=$('#fppw').value;
  $('#modalBody').innerHTML='<div class="dim">正在启动 flatpak 升级…</div>';
  try{
    const r=await fetch('/api/flatpak/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,password:pw,use_sudo:needSudo})});
    const j=await r.json();
    if(j.need_root){$('#modalBody').innerHTML='<div style="color:#f87171">勾选中包含系统级安装（如 Brave），需要 sudo 密码</div><button class="ghost" onclick="openFlatpakUpg()">← 返回</button>';return}
    window._FPJOB=j.id;renderJob('flatpak',j.id,0);
  }catch(e){$('#modalBody').innerHTML='<div style="color:#f87171">启动失败: '+esc(e)+'</div>'}
}
$('#q').addEventListener('input',e=>{state.q=e.target.value;renderTable(visible())});
document.querySelectorAll('.chip').forEach(c=>c.addEventListener('click',()=>{
  document.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));c.classList.add('on');
  state.type=c.dataset.t;state.env=null;renderAll();
}));
$('#onlylocal').addEventListener('change',e=>{state.onlyLocal=e.target.checked;renderAll()});
$('#showsys').addEventListener('change',e=>{state.showSys=e.target.checked;renderAll()});
document.querySelectorAll('th[data-k]').forEach(th=>th.addEventListener('click',()=>{
  const k=th.dataset.k;if(state.sortK===k)state.sortD*=-1;else{state.sortK=k;state.sortD=1}renderTable(visible());
}));
$('#pvbadge').textContent='v'+PAGE_VERSION; // 状态栏显示页面 JS 版本（诊断用）
poll();setInterval(poll,10000); // 实时刷新：每10秒轮询一次
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="pkgtool 浏览器可视化")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=int, default=300,
                    help="后台自动重新采集间隔秒数（默认300，0=关闭）")
    args = ap.parse_args()
    threading.Thread(target=collect, daemon=True).start()  # 启动即开始采集
    if args.interval > 0:
        def auto_loop():
            while True:
                time.sleep(args.interval)
                collect()  # 已有并发保护，正在采就跳过
        threading.Thread(target=auto_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"pkgtool UI: http://localhost:{args.port}  (局域网: http://<本机IP>:{args.port})"
          f"  自动重新采集: 每{args.interval}s")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()