# pkgtool

Debian 系本地软件包**盘点与管理**工具（零第三方依赖）。

一个 TUI + CLI 工具，统一盘点和管理你机器上的五种软件包格式：
**deb（apt/dpkg）· snap · flatpak · 如意玲珑（linyaps）· AppImage**。

```bash
pkgtool          # 裸跑进入交互界面
```

```
 [1]全部包  [2]可升级  [3]散落包文件  [4]仓库搜索  [5]磁盘回收
```

## 它能做什么

- **盘点**：安装通道（apt / dpkg -i / 镜像自带）、来源、体积、独占依赖、
  首次安装时间，一个界面看清全机的软件资产
- **分级披露**：默认只显示你真正能用的软件，`s` 键逐层展开到依赖和系统组件
- **装卸**：卸载先 dry-run 预览（含依赖级联和等效命令），确认才执行；
  可选清理主目录配置/缓存残留；`pkgtool run` / TUI `o` 键直接启动应用
- **仓库搜索**：apt 源 / Snap Store / flathub 三源搜索与安装，标注已装
- **磁盘回收**：散落包文件、用户缓存（~/.cache）、apt 下载缓存、snap 旧修订、
  flatpak/玲珑未引用运行时、conda 包缓存——先列清单再删，支持回收站模式
- **离线优先**：各后端优先读文件系统缓存（dpkg status、flatpak 目录、
  ll-cli --json），CLI 查询只是增强，失败自动降级；单后端故障不影响整体

## 安装

```bash
pipx install pkg-tool        # 推荐
pip install pkg-tool         # 或者
```

也可以从 Release 页下载：
- `pkgtool_x.y.z_all.deb` —— `sudo dpkg -i` 安装
- `pkgtool-x.y.z.pyz` —— 单文件，`chmod +x` 后直接运行，只要 python3

安装后命令都是 `pkgtool`。

## 常用子命令

```bash
pkgtool                      # TUI（六个视图：全部包/可升级/散落/搜索/回收）
pkgtool list                 # 列出软件（--all 看全部层级，--loose 看散落文件）
pkgtool info <名字>           # 单包详情：通道、来源、体积、启动命令
pkgtool search <关键词>       # 三源搜索
pkgtool install -s snap firefox   # 从指定源安装（-s 必须：三源命名空间会重名）
pkgtool remove <名字>         # 卸载（先预览再确认）
pkgtool run <名字>           # 启动应用
pkgtool clean --list         # 磁盘回收清单（加 -y 直接删）
pkgtool summary              # 按格式/通道/来源/类别汇总
```

需要 root 的操作（安装/卸载/升级）不在进程里处理密码，由 `sudo` 在终端上
直接提示；`--yes` 类参数跳过确认前请先看清预览输出。

## 兼容性

- Python ≥ 3.10（标准库实现，无任何第三方依赖）
- Debian 12+ / Ubuntu 22.04+ / deepin 23+ 均可
- 没装 snap/flatpak/玲珑 对应的后端自动不启用；散落 AppImage 嗅探
  纯被动（只找 squashfs 超块，不执行目标文件）
- 适配 deepin 等发行版的本地化 apt：模拟输出解析固定 C locale

## 配置

系统路径可用环境变量整体重定向或逐项覆盖（chroot/容器/测试友好）：

```bash
PKGTOOL_ROOT=/mnt/chroot pkgtool list      # 整体重定向
PKGTOOL_SNAP_MOUNT_DIR=/snap pkgtool ...   # 单项覆盖
pkgtool config                             # 查看当前生效的全部路径与阈值
```

XDG 标准变量（`XDG_DATA_HOME` / `XDG_CACHE_HOME`）全部遵循。

## 开发

```bash
python3 -m unittest discover -s tests    # 72 个单测，纯函数层全覆盖
python3 scripts/build_pyz.py             # 打单文件发行版 dist/*.pyz
```

分层架构（依赖自上而下）：`config/base` 内核 → `apt/` 与 `backends/`
数据源 → `classify/remove/clean/upgrade` 安全层 → `inventory` 唯一数据
入口 → `report/cli/app` 输出与界面。新增包格式 = 新建
`backends/<fmt>.py` 实现 `Backend` + 注册一行。

## 许可

MIT
