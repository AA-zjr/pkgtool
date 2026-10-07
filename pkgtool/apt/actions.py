"""pkgtool.apt.actions — 需要特权的写操作。

统一提权入口：原实现有三份互不复用的特权执行路径（Web UI 的 sudo -S apt 升级、
sudo -S flatpak 升级、safety.py 的 pkexec bash -c），这里收敛成一个
run_privileged()，remove.py 与各后端的升级动作都走它。

安全约束：
  · 密码只经 stdin 传给 sudo，不进 argv（argv 在 /proc/<pid>/cmdline 全局可见）
  · 一律用 argv 列表调用，不经过 shell
  · 包名/版本先过 base.is_safe_name 白名单，拒绝前导 '-' 造成的参数注入
"""
import glob
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field

from ..base import is_safe_name
from ..config import CFG

_SUDO_NOPASS = None


@dataclass
class Result:
    """一次写操作的结果。command 是不含密码的可复现命令（供失败时提示手动执行）。"""
    ok: bool
    returncode: int = 0
    output: str = ""
    error: str = ""
    file: str = ""
    command: list = field(default_factory=list)

    @property
    def command_text(self):
        return " ".join(shutil.quote(c) for c in self.command)


def is_root():
    return os.geteuid() == 0


def sudo_nopass():
    """是否有免密 sudo（结果缓存）。没有时特权命令会在终端上交互提示。"""
    global _SUDO_NOPASS
    if _SUDO_NOPASS is None:
        if is_root():
            _SUDO_NOPASS = True
            return True
        try:
            _SUDO_NOPASS = subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            _SUDO_NOPASS = False
    return _SUDO_NOPASS


def privileged_argv(argv, password=None):
    """套上提权前缀。已是 root → 原样；给了密码 → sudo -S；否则裸 sudo 走终端提示。"""
    if is_root():
        return list(argv)
    if password:
        return ["sudo", "-S", "-p", "", *argv]
    return ["sudo", *argv]


def _kill(p, own_session):
    """中止子进程。
    own_session=True 时子进程自成一个会话，可以 killpg 干净地带走整棵树；
    False 时它和我们同组，killpg 会把自己一起杀掉，只能 terminate 它本身
    （apt-get 收到 SIGTERM 会自行收拾 dpkg）。"""
    try:
        if own_session:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        else:
            p.terminate()
        p.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            if own_session:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            else:
                p.kill()
        except OSError:
            pass


def _error_summary(rc, lines):
    """失败原因：取输出的最后一行非空内容。
    只报"退出码 1"等于没报——sudo 认证失败、apt 依赖冲突这些真正的原因
    都在输出里，用户看到的却是一个光秃秃的数字。"""
    for line in reversed(lines):
        line = line.strip()
        if line:
            return f"{line[:200]}（退出码 {rc}）"
    return f"退出码 {rc}"


def run_privileged(argv, password=None, timeout=None, on_line=None, cfg=CFG):
    """执行特权命令并逐行回显。Ctrl-C 中止（替代原 Web UI 的 cancel 接口）。

    只有"密码经 stdin 喂进去"这种非交互场景才新建会话（那样能 killpg 整棵树）。
    交互式必须留在当前会话里：start_new_session 会 setsid() 切断控制终端，
    sudo 就再也打不开 /dev/tty 读密码，直接报
    "A terminal is required to authenticate"——密码提示根本出不来。"""
    cmd = privileged_argv(argv, password)
    shown = list(argv) if is_root() else ["sudo", *argv]   # 可复现命令，永不含密码
    own_session = bool(password)
    lines = []
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE if password else None,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=own_session)
    except OSError as e:
        return Result(ok=False, returncode=-1, error=f"{type(e).__name__}: {e}",
                      command=shown)
    try:
        if password:
            try:
                p.stdin.write(password + "\n")
                p.stdin.close()
            except OSError:
                pass          # sudo 可能已因缓存凭据直接放行
        for line in p.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_line:
                on_line(line)
        rc = p.wait(timeout=timeout)
    except KeyboardInterrupt:
        _kill(p, own_session)
        return Result(ok=False, returncode=-2, output="\n".join(lines),
                      error="已中止（Ctrl-C）", command=shown)
    except subprocess.TimeoutExpired:
        _kill(p, own_session)
        return Result(ok=False, returncode=-3, output="\n".join(lines),
                      error=f"超时（{timeout}s）", command=shown)
    except OSError as e:
        _kill(p, own_session)
        return Result(ok=False, returncode=-1, output="\n".join(lines),
                      error=f"{type(e).__name__}: {e}", command=shown)
    return Result(ok=rc == 0, returncode=rc,
                  output="\n".join(lines)[-cfg.output_tail_chars:],
                  error="" if rc == 0 else _error_summary(rc, lines),
                  command=shown)


def run_plain(argv, cwd=None, timeout=None, cfg=CFG):
    """非特权命令（apt-get download / dpkg-deb 一类）。"""
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout or cfg.timeout_query)
    except subprocess.TimeoutExpired:
        return Result(ok=False, returncode=-3, error=f"超时（{timeout}s）", command=argv)
    except OSError as e:
        return Result(ok=False, returncode=-1, error=f"{type(e).__name__}: {e}", command=argv)
    return Result(ok=p.returncode == 0, returncode=p.returncode,
                  output=((p.stdout or "") + (p.stderr or "")).strip()[-cfg.output_tail_chars:],
                  error=(p.stderr or "").strip()[-500:], command=argv)


def download(name, version="", dest_dir=None, cfg=CFG):
    """apt-get download（非 root 可用）→ .deb 落到 dest_dir（默认 ~/Downloads）。"""
    if not is_safe_name(name) or (version and not is_safe_name(version)):
        return Result(ok=False, error=f"包名或版本非法：{name}={version}")
    spec = f"{name}={version}" if version else name
    dest_dir = dest_dir or cfg.download_dir
    tmp = tempfile.mkdtemp(prefix="pkgtool_dl_")
    try:
        r = run_plain(["apt-get", "download", spec], cwd=tmp,
                      timeout=cfg.timeout_download, cfg=cfg)
        debs = glob.glob(os.path.join(tmp, "*.deb"))
        if not r.ok or not debs:
            tail = [ln for ln in (r.output or r.error).splitlines() if ln.strip()]
            return Result(ok=False, error=(tail[-1] if tail else "下载失败")[:300],
                          command=r.command)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(debs[0]))
        shutil.move(debs[0], dest)
        return Result(ok=True, file=dest, command=r.command)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # 原实现把临时目录留在 /tmp 不清


def upgrade(name, version="", password=None, on_line=None, cfg=CFG):
    """apt-get install -y 到指定版本（version 为空则装候选版本）。"""
    if not is_safe_name(name) or (version and not is_safe_name(version)):
        return Result(ok=False, error=f"包名或版本非法：{name}={version}")
    spec = f"{name}={version}" if version else name
    return run_privileged(["apt-get", "install", "-y", spec], password=password,
                          timeout=cfg.timeout_upgrade, on_line=on_line, cfg=cfg)
