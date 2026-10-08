"""pkgtool.config — 路径与可调参数的唯一来源。

硬编码治理三条规则：
  1. 系统路径一律由 root 前缀派生，PKGTOOL_ROOT=/mnt/chroot 可整体重定向（chroot/容器/测试）
  2. 单个路径可用 PKGTOOL_<字段名大写> 覆盖
  3. 时间窗、超时、缓存 TTL、截断长度等阈值集中在此，不散落到各模块
主目录相关路径不加 root 前缀：永远取真实用户的 home（见 user_home）。
"""
import os
import pwd
from dataclasses import dataclass, fields


def user_home():
    """真实用户主目录：走 passwd 数据库，不受 HOME 环境变量重写影响。"""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


# 支持 root 前缀与环境变量覆盖的系统路径字段
_PATH_FIELDS = (
    "dpkg_status", "dpkg_info_dir", "dpkg_log_glob", "apt_history_glob",
    "apt_lists_dir", "apt_archives_dir", "flatpak_system_dir",
    "flatpak_exports_bin", "snap_store_dir", "snap_mount_dir",
)


def _resolve(root, default, env_name):
    override = os.environ.get(env_name)
    if override:
        return override
    if root == "/":
        return default
    return os.path.join(root, default.lstrip("/"))


@dataclass(frozen=True)
class Config:
    root: str = "/"

    # ---- dpkg ----
    dpkg_status: str = "/var/lib/dpkg/status"
    dpkg_info_dir: str = "/var/lib/dpkg/info"
    dpkg_log_glob: str = "/var/log/dpkg.log*"

    # ---- apt ----
    apt_history_glob: str = "/var/log/apt/history.log*"
    apt_lists_dir: str = "/var/lib/apt/lists"
    apt_archives_dir: str = "/var/cache/apt/archives"
    ext_states_paths: tuple = ("/var/lib/apt/extended_states",
                               "/var/lib/dpkg/extended_states")
    index_arches: tuple = ("amd64", "all")
    index_skip_arch_re: str = r"_(arm64|armhf|arm64ec|i386|ppc64el|riscv64|s390x)_binary"
    known_components: frozenset = frozenset(
        {"main", "universe", "multiverse", "restricted", "contrib",
         "non-free", "non-free-firmware", "upstream"})

    # ---- flatpak ----
    flatpak_system_dir: str = "/var/lib/flatpak"
    flatpak_exports_bin: str = "/usr/lib/flatpak/exports/bin"

    # ---- snap ----
    snap_store_dir: str = "/var/lib/snapd/snaps"
    snap_mount_dir: str = "/snap"

    # ---- AppImage ----
    appimage_app_dirs: tuple = ("Applications",)        # 主目录下视为"已安置"
    appimage_system_dirs: tuple = ("/opt", "/usr/local/bin")
    appimage_download_dirs: tuple = ("下载", "Downloads", "桌面", "Desktop")
    appimage_scan_limit: int = 32 * 1024 * 1024         # 找 squashfs 超块只读前 32MB
    appimage_max_exes: int = 8
    appimage_xdg_wrappers: frozenset = frozenset(
        {"xdg-open", "xdg-mime", "xdg-settings"})       # 不是应用本体，排除

    # ---- 文件系统扫描 ----
    bin_dirs: tuple = ("/bin", "/sbin", "/usr/bin", "/usr/sbin")
    scan_system_roots: tuple = ("/opt", "/usr/local/bin", "/tmp")
    scan_maxdepth: int = 3
    prune_dirs: frozenset = frozenset(
        {".cache", ".config", ".local", "node_modules", "__pycache__",
         ".venv", "venv", "snap", ".npm", ".bun", ".rustup"})

    # ---- 卸载残留扫描 ----
    residue_home_dirs: tuple = (".config", ".cache", ".local/share",
                                ".local/state", ".local/lib", ".local/bin")
    residue_system_roots: tuple = ("/opt",)
    residue_min_id_len: int = 3      # 短于此的标识不参与前缀匹配，避免误伤
    # 包管理器/运行时自己的数据根：里面是应用本体而非配置残留，永不删除
    residue_protected: tuple = (
        ".local/share/flatpak",      # 用户级 flatpak 安装根（repo + db）
        ".local/share/snapd",
        ".local/share/containers",
        ".local/share/docker",
        ".local/share/pipx",
        ".local/share/virtualenvs",
        ".local/share/venv",
    )

    # ---- Python 环境探测 ----
    conda_dir_names: tuple = ("miniconda3", "anaconda3", "miniforge3")
    conda_scan_roots: tuple = ("/opt",)
    python_scan_maxdepth: int = 4    # 找 conda-meta / pyvenv.cfg 的最大深度
    python_system_root: str = "/usr/local"
    # 注意：与 prune_dirs 不同——这里不能剪掉 .venv/.local，那正是要找的目标
    pip_prune_dirs: frozenset = frozenset(
        {".cache", "node_modules", "__pycache__", ".npm", ".bun", ".rustup"})

    # ---- 磁盘回收（clean 子命令）----
    pip_cache_dir: str = ".cache/pip"   # 相对 home，纯缓存，删了只会重新下载
    conda_pkgs_subdir: str = "pkgs"     # <conda 发行版根>/pkgs 是包缓存

    # ---- 判定阈值 ----
    birth_margin_hours: int = 24     # 日志最早事件 + 此窗口内安装 = 镜像自带
    index_cache_ttl: float = 300.0   # apt 索引解析缓存（apt update 后自动失效）
    env_cache_ttl: float = 60.0      # Python 环境探测缓存

    # ---- 远程目录搜索（catalog）----
    timeout_catalog: int = 30        # snap find / flatpak remote-ls 的超时
    catalog_cache_ttl: float = 300.0  # flatpak 全量清单与 snap 查询结果的缓存

    # ---- 子进程超时（秒）----
    timeout_query: int = 30          # snap list / flatpak list / dpkg-deb 一类查询
    timeout_mark: int = 120          # apt-mark
    timeout_dry_run: int = 60        # apt-get remove -s
    timeout_remove: int = 180
    timeout_download: int = 900
    timeout_upgrade: int = 900

    # ---- 输出 ----
    output_tail_chars: int = 2000    # 回显命令输出时保留的尾部字符数
    exec_list_limit: int = 6         # pip 记录里最多列几个命令
    top_dirs_limit: int = 4          # install_path 里最多列几个顶层目录

    @classmethod
    def from_env(cls):
        root = os.environ.get("PKGTOOL_ROOT", "").rstrip("/") or "/"
        kw = {"root": root}
        for name in _PATH_FIELDS:
            kw[name] = _resolve(root, getattr(cls, name), "PKGTOOL_" + name.upper())
        ext = os.environ.get("PKGTOOL_EXT_STATES")
        if ext:
            kw["ext_states_paths"] = tuple(
                _resolve(root, p, "") for p in ext.split(":") if p)
        for name in ("scan_system_roots", "residue_system_roots", "bin_dirs"):
            env = os.environ.get("PKGTOOL_" + name.upper())
            if env:
                kw[name] = tuple(p for p in env.split(":") if p)
        birth = os.environ.get("PKGTOOL_BIRTH_MARGIN_HOURS")
        if birth and birth.isdigit():
            kw["birth_margin_hours"] = int(birth)
        return cls(**kw)

    # ---- 派生路径：依赖运行时 home，不做成字段 ----

    @property
    def home(self):
        return user_home()

    @property
    def flatpak_user_dir(self):
        return os.path.join(user_home(), ".local", "share", "flatpak")

    @property
    def download_dir(self):
        """apt-get download 的落盘目录（原 UI 里写死 ~/Downloads）。"""
        return os.environ.get("PKGTOOL_DOWNLOAD_DIR") or \
            os.path.join(user_home(), "Downloads")

    @property
    def scan_roots(self):
        """散落包文件的扫描根：主目录 + 存在的系统目录。"""
        return (user_home(),) + tuple(
            p for p in self.scan_system_roots if os.path.isdir(p))

    @property
    def residue_roots(self):
        out = [os.path.join(user_home(), d) for d in self.residue_home_dirs]
        out += [p for p in self.residue_system_roots if os.path.isdir(p)]
        return out

    @property
    def residue_home_roots(self):
        """只含主目录内的残留扫描根（不含 /opt 一类系统目录）。"""
        return [os.path.join(user_home(), d) for d in self.residue_home_dirs]

    @property
    def python_scan_roots(self):
        """找 conda-meta / pyvenv.cfg 的扫描根：主目录 + 系统目录。"""
        return (user_home(),) + tuple(self.conda_scan_roots)

    @property
    def pip_cache_path(self):
        return os.path.join(user_home(), self.pip_cache_dir)

    @property
    def trash_dir(self):
        """freedesktop 回收站根目录（--trash 时用）。"""
        return os.path.join(user_home(), ".local", "share", "Trash")

    def dump(self):
        """→ {字段: 值}，供 `pkgtool list -v` 一类诊断输出。"""
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        for name in ("home", "flatpak_user_dir", "download_dir",
                     "scan_roots", "residue_roots"):
            out[name] = getattr(self, name)
        return {k: (sorted(v) if isinstance(v, frozenset) else v)
                for k, v in out.items()}


CFG = Config.from_env()
