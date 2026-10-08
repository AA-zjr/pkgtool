"""pkgtool.remove — 卸载安全层：残留扫描 + dry-run 预览 + 执行。

相对原 safety.py 的三处修正：
  1. 执行必须复用预览算出的残留清单。原实现 execute_remove() 重新扫一遍残留，
     用户确认的清单和真正 rm -rf 的清单不是同一份（TOCTOU）。现在 preview()
     产出一个 Plan，execute() 只按 Plan 执行，所见即所得。
  2. 残留清理默认关闭。原 UI 里"彻底清理"复选框默认勾选，等于默认删用户数据；
     现在要显式传 purge_residues=True（CLI 的 --purge-residues）。
  3. 不再拼 shell 字符串。原实现把命令拼成一条 "a && b && rm -rf x" 交给
     pkexec bash -c，靠 shlex.quote 兜底；现在每步都是 argv 列表直接 exec，
     且主目录下的残留用普通用户权限删（本来就不需要 root）。
"""
import glob
import os
import shlex
import subprocess
from dataclasses import dataclass, field

from .apt import actions
from .base import delete_paths, file_size_mb, is_safe_name, is_under_home
from .classify import is_removable
from .config import CFG
from .labels import block_reason


@dataclass
class Plan:
    """一次卸载的完整执行计划：由 preview() 产出，execute() 照单执行。"""
    ok: bool
    pkg_type: str = ""
    name: str = ""
    target: str = ""
    will_remove: list = field(default_factory=list)   # dry-run 报出的包名
    residues: list = field(default_factory=list)      # [(path, size_mb)]
    freed_mb: float = 0.0
    steps: list = field(default_factory=list)         # 需特权的 argv 列表
    plain_steps: list = field(default_factory=list)   # 无需特权的 argv（用户级 flatpak 卸载）
    user_paths: list = field(default_factory=list)    # 主目录内，普通权限可删
    error: str = ""

    @property
    def command_text(self):
        """等效命令，供复制粘贴到终端手动执行。"""
        out = ["sudo " + " ".join(shlex.quote(a) for a in argv) for argv in self.steps]
        out += [" ".join(shlex.quote(a) for a in argv) for argv in self.plain_steps]
        out += [f"rm -rf {shlex.quote(p)}" for p in self.user_paths]
        return " && ".join(out)


def _target(rec):
    """卸载目标：appimage 是文件路径，其余是包名/app-id。
    原实现在 preview 和 execute 里各写一遍这个三元表达式，容易改漏一处。"""
    if rec.pkg_type == "appimage":
        return rec.extra.get("found_at") or rec.install_path or rec.name
    return rec.name


def _residue_roots(rec, cfg):
    """残留扫描范围。
    受包管理器管理的格式（deb/snap/flatpak/linyap）只扫主目录：卸载命令
    自己会删掉它装进系统目录的文件，再把 /opt/wechat 当"残留"rm -rf 一遍
    是重复计数，还会把包本体的体积算进"可回收空间"误导用户（实测 755MB）。
    只有 AppImage 这种没人管的文件才需要连系统目录一起扫。"""
    managed = (rec.pkg_type in ("deb", "snap", "linyap")
               or rec.pkg_type.startswith("flatpak"))
    return cfg.residue_home_roots if managed else cfg.residue_roots


def _matches(entry, exact_ids, prefix_ids, min_len):
    """目录/文件名是否属于这个包。两级匹配：

      exact_ids   包名 + 可执行文件名 + desktop ID，只做精确（含忽略大小写、
                  去点前缀）匹配。可执行名往往很通用——ubuntu-session 就带一个
                  /usr/bin/ubuntu，拿它做前缀匹配会把 ~/.config/ubuntu-insights、
                  ~/.cache/ubuntu-pro 这些别的包的数据算成残留，rm -rf 就删错了。
      prefix_ids  只有包名和 desktop ID 这类有区分度的标识，才允许
                  「前缀+连字符」和「按点号拆段」匹配。拆段是必须的：大量应用用
                  反向 DNS 作配置目录名（clash-verge 的是
                  io.github.clash-verge-rev.clash-verge-rev），整体匹配找不到。
                  要求连字符分隔且标识足够长，否则 "go" 会命中 "google"。
    exact_ids 里的 desktop ID 同样可能很通用（ubuntu-session 装的是
    ubuntu.desktop，ubuntu-settings 装的是 gnome-initial-setup.desktop），
    所以前缀/拆段匹配只认包名这一个标识。
    """
    stripped = entry.lstrip(".")
    for cand in (entry, stripped):
        low = cand.lower()
        for i in exact_ids:
            if cand == i or low == i.lower():
                return True
    segs = [s for s in stripped.split(".") if s]
    for cand in (entry, stripped, *segs):
        low = cand.lower()
        for i in prefix_ids:
            if len(i) < min_len:
                continue
            if cand.startswith(i + "-") or low.startswith(i.lower() + "-"):
                return True
    return False


def _is_protected(path, cfg):
    """包管理器自己的数据根（如用户级 flatpak 安装目录）绝不当残留删：
    里面装的是应用本体，不是配置缓存。删掉等于清空所有用户级 flatpak。
    用户级 flatpak 目录随 XDG_DATA_HOME 走，单独比对，不能只靠
    residue_protected 里的字面相对路径。"""
    home = os.path.abspath(cfg.home)
    target = os.path.abspath(path)
    protected = {os.path.join(home, p) for p in cfg.residue_protected}
    protected.add(os.path.abspath(cfg.flatpak_user_dir))
    return target in protected


def find_residues(rec, cfg=CFG):
    """扫描残留目录/文件 → [(path, size_mb)]。范围见 _residue_roots。"""
    exact_ids = {rec.name}
    for e in rec.executables:
        b = os.path.basename(e)
        if len(b) >= cfg.residue_min_id_len:
            exact_ids.add(b)
    desktop_id = rec.extra.get("desktop_id", "")
    if desktop_id:
        exact_ids.add(desktop_id)
    prefix_ids = {rec.name}

    out = []
    for root in _residue_roots(rec, cfg):
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for entry in entries:
            if not _matches(entry, exact_ids, prefix_ids, cfg.residue_min_id_len):
                continue
            full = os.path.join(root, entry)
            if _is_protected(full, cfg):
                continue
            out.append((full, file_size_mb(full)))
    return out


def _cached_debs(name, cfg):
    """apt 缓存里该包的 .deb（彻底清理时一并删掉，需要 root）。"""
    paths = []
    for ext in (".deb", ".ddeb"):
        paths += sorted(glob.glob(os.path.join(cfg.apt_archives_dir, f"{name}_*{ext}")))
    return paths


def _apt_dry_run(name, autoremove, cfg):
    """apt-get remove -s → (会被一并移除的包名列表, 错误信息)。

    标志必须与真正执行的命令一致：execute 跑的是 purge --autoremove，
    dry-run 就必须也带 --autoremove。少了它，存在反向依赖的包会严重低报——
    实测 node-css-loader 不带 autoremove 只报 1 个包，带上后是 397 个
    （npm 依赖它，npm 整棵自动安装树随之孤立），用户会在错误的计划上确认。

    用 remove -s 而不是 purge -s：purge 的模拟输出是本地化文本，Remv 行不稳定；
    两者算出的移除集合一致，真正执行时才用 purge。
    固定 LC_ALL=C：Remv 行是解析目标，deepin 等发行版的 apt 带本地化补丁，
    zh_CN 环境下不能赌它不翻译。"""
    argv = ["apt-get", "remove", "-s"]
    if autoremove:
        argv.append("--autoremove")
    try:
        p = subprocess.run(argv + [name], capture_output=True, text=True,
                           timeout=cfg.timeout_dry_run,
                           env=dict(os.environ, LC_ALL="C", LANG="C"))
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"{type(e).__name__}: {e}"
    will = [ln.split()[1].split(":")[0] for ln in p.stdout.splitlines()
            if ln.startswith("Remv ") and len(ln.split()) > 1]
    err = p.stderr.strip()[-500:] if p.returncode else ""
    return (will or [name]), err


def _deb_steps(rec, target, purge_residues, autoremove, cfg):
    argv = ["apt-get", "purge", "-y"]
    if autoremove:
        argv.append("--autoremove")
    steps = [argv + [target]]
    if purge_residues:
        cached = _cached_debs(target, cfg)
        if cached:
            steps.append(["rm", "-f", "--", *cached])
    return steps


def _snap_steps(rec, target, purge_residues):
    argv = ["snap", "remove"]
    if purge_residues:
        argv.append("--purge")       # 连保存数据一起删
    return [argv + [target]]


def _flatpak_steps(rec, target, purge_residues):
    """→ (argv, user)。用户级安装（installation=user）不需要 root——
    原实现一律套 sudo，在无终端环境下会因 sudo 无法认证而整个失败。"""
    argv = ["flatpak", "uninstall", "-y"]
    if purge_residues:
        argv.append("--delete-data")
    user = rec.extra.get("installation") == "user"
    if user:
        argv.append("--user")
    arch, branch = rec.extra.get("arch"), rec.extra.get("branch")
    if arch:                         # --arch 是 uninstall 的合法选项
        argv.append(f"--arch={arch}")
    if branch:
        # uninstall 没有 --branch 选项，branch 要编码进 ref：flatpak 的部分
        # 引用语法 名称//分支（写成 --branch=stable 会被直接拒绝）
        target = f"{target}//{branch}"
    return argv + [target], user


def _linyap_steps(rec, target):
    """玲珑卸载只删应用层；卸载后残留的未引用 base/runtime 由
    `ll-cli prune` 处理（磁盘回收里的「玲珑未引用运行时」条目）。"""
    argv = ["ll-cli", "uninstall"]
    if rec.extra.get("module") and rec.extra["module"] != "binary":
        argv.append(f"--module={rec.extra['module']}")
    return [argv + [target]]


def preview(rec, purge_residues=False, autoremove=True, cfg=CFG):
    """dry-run + 残留扫描（无需 root）→ Plan。非 APP 类直接拒绝。"""
    if not is_removable(rec):
        return Plan(ok=False, pkg_type=rec.pkg_type, name=rec.name,
                    error=block_reason(rec.pkg_class))
    t, target = rec.pkg_type, _target(rec)
    if t != "appimage" and not is_safe_name(target):
        return Plan(ok=False, pkg_type=t, name=rec.name,
                    error=f"目标名非法，拒绝操作：{target!r}")

    will_remove, err = [target], ""
    steps, plain_steps = [], []
    if t == "deb":
        will_remove, err = _apt_dry_run(target, autoremove, cfg)
        steps = _deb_steps(rec, target, purge_residues, autoremove, cfg)
    elif t == "snap":
        steps = _snap_steps(rec, target, purge_residues)
    elif t.startswith("flatpak"):
        argv, user = _flatpak_steps(rec, target, purge_residues)
        (plain_steps if user else steps).append(argv)
    elif t == "linyap":
        steps = _linyap_steps(rec, target)
    elif t == "appimage":
        steps = []
    else:
        return Plan(ok=False, pkg_type=t, name=rec.name,
                    error=f"不支持卸载的包类型：{t}")

    residues = find_residues(rec, cfg) if purge_residues else []
    user_paths = []
    for path, _size in residues:
        if is_under_home(path, cfg):
            user_paths.append(path)          # 属于当前用户，不需要 root
        else:
            steps.append(["rm", "-rf", "--", path])
    if t == "appimage":
        # 便携应用本体就是一个文件：主目录内普通权限删，/opt 一类才需要特权
        if is_under_home(target, cfg):
            user_paths.append(target)
        else:
            steps.append(["rm", "-f", "--", target])

    return Plan(ok=not err, pkg_type=t, name=rec.name, target=target,
                will_remove=will_remove, residues=residues,
                freed_mb=round(sum(s for _p, s in residues), 1),
                steps=steps, plain_steps=plain_steps,
                user_paths=user_paths, error=err)


def execute(plan, password=None, on_line=None, cfg=CFG):
    """照 Plan 执行：先特权步骤（任一步失败即停，不留半删状态），再跑无需
    特权的命令步骤，最后删主目录内的残留。只报告实际删掉的路径。"""
    if not plan.ok:
        return actions.Result(ok=False, error=plan.error or "预览未通过，拒绝执行")
    outputs = []

    def run(argv, privileged):
        if privileged:
            return actions.run_privileged(argv, password=password,
                                          timeout=cfg.timeout_remove,
                                          on_line=on_line, cfg=cfg)
        return actions.run_plain(argv, timeout=cfg.timeout_remove, cfg=cfg)

    tagged = [(argv, True) for argv in plan.steps] \
        + [(argv, False) for argv in plan.plain_steps]
    for argv, privileged in tagged:
        r = run(argv, privileged)
        outputs.append(r.output)
        if not r.ok:
            return actions.Result(ok=False, returncode=r.returncode,
                                  output="\n".join(outputs), error=r.error, command=argv)
    removed, failed = delete_paths(plan.user_paths)
    outputs += [f"已删除残留 {p}" for p in removed]
    return actions.Result(ok=not failed, output="\n".join(o for o in outputs if o),
                          error="; ".join(failed), file=",".join(removed),
                          command=tagged[0][0] if tagged else [])
