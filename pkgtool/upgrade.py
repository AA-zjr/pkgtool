"""pkgtool.upgrade — 跨格式升级分派。

不同包格式的升级方式完全不同，这层负责"给定一条记录，该用哪个命令升"，
CLI 与交互界面共用（原 Web UI 里 apt 和 flatpak 是两份几乎一样的后台任务代码）。
pip 包不做代执行：它属于某个具体环境，用当前解释器去升级会装错地方，
只给出建议命令。
"""
from .apt import actions
from .config import CFG


def plan(rec):
    """→ (argv, privileged)；返回 None 表示该格式没有系统级更新通道。"""
    t, n = rec.pkg_type, rec.name
    if t == "deb":
        spec = f"{n}={rec.candidate}" if rec.candidate else n
        return ["apt-get", "install", "-y", spec], True
    if t == "snap":
        return ["snap", "refresh", n], True
    if t.startswith("flatpak"):
        user = rec.extra.get("installation") == "user"
        argv = ["flatpak", "update", "-y"] + (["--user"] if user else []) + [n]
        return argv, not user          # 用户级安装不需要 root
    return None


def run(rec, on_line=None, cfg=CFG):
    """执行升级 → actions.Result。无更新通道时返回带建议文案的失败结果。"""
    p = plan(rec)
    if p is None:
        from .labels import update_advice
        return actions.Result(ok=False, error=update_advice(rec))
    argv, privileged = p
    if privileged:
        return actions.run_privileged(argv, timeout=cfg.timeout_upgrade,
                                      on_line=on_line, cfg=cfg)
    return actions.run_plain(argv, timeout=cfg.timeout_upgrade, cfg=cfg)


def system_wide(on_line=None, cfg=CFG):
    """整机升级（apt-get upgrade）。"""
    return actions.run_privileged(["apt-get", "upgrade", "-y"],
                                  timeout=cfg.timeout_upgrade,
                                  on_line=on_line, cfg=cfg)
