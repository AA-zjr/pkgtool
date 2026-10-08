# pkgtool

Debian 系本地软件包盘点与管理工具。纯 Python 标准库实现，无第三方依赖。

支持 deb / snap / flatpak / 玲珑（linyaps）/ AppImage 五种格式的盘点、
启动、安装、卸载、升级和磁盘清理。命令行 + 终端界面。

## 环境

- Python >= 3.10
- Debian 12+ / Ubuntu 22.04+ / deepin 23+
- 没装的包管理器对应功能自动禁用

**测试声明：只在 Ubuntu 26.04 LTS 上完整测试过**，包管理版本：

```
apt 3.2.0 / dpkg 1.23.7 / snap 2.77.1 / flatpak 1.16.6 / ll-cli 1.12.3
```

其他环境未经测试，出问题欢迎提 issue。

## 安装

```bash
pipx install pkg-tool
```

命令是 `pkgtool`。Release 页另有 `.deb` 包和单文件 `.pyz`（python3 直接运行）。

## 使用

```
pkgtool                       进入交互界面
pkgtool list                  列出软件；--all 显示全部层级；--loose 只看散落包文件
pkgtool info 名字              单包详情
pkgtool search 关键词          搜 apt / Snap Store / flathub
pkgtool install -s 来源 名字   安装；来源必填（apt/snap/flatpak 会重名）
pkgtool remove 名字            卸载，先预览再确认
pkgtool run 名字               启动
pkgtool clean --list          磁盘回收清单；-y 直接删
pkgtool config                查看当前配置
```

需要 root 的操作由 `sudo` 在终端上提示密码，程序不经手。

已知限制：玲珑只做了盘点/启动/卸载/升级，没接商店搜索；散落 AppImage
靠文件嗅探识别（不执行目标文件），可能漏报。

## 项目结构

```
pkgtool/
  config.py        路径与阈值的唯一来源（PKGTOOL_* 环境变量在此覆盖）
  base.py          统一数据模型 PackageRecord 与后端契约 Backend
  compress.py      压缩日志文件的统一读取（gz/xz/bz2）
  labels.py        枚举 code → 中文文案的唯一来源
  apt/             deb 数据源：apt 索引解析、dpkg 状态、安装日志、
                   依赖图、Debian 版本比较、特权执行入口
  backends/        各格式采集器：deb / snap / flatpak / linyap / appimage
  classify.py      卸载风险分类（只有"软件"允许删）与显示层级
  inventory.py     采集编排：跑后端 → 分类 → 去重，唯一数据入口
  remove.py        卸载：dry-run 预览产出 Plan，执行照单进行
  clean.py         磁盘回收：散落文件 / 各类缓存 / 未引用运行时
  upgrade.py       跨格式升级分派
  launch.py        应用启动（各格式收敛在一个模块）
  catalog.py       apt / Snap Store / flathub 三源搜索与安装
  report.py        输出层：表格 / CSV / JSON / 详情
  cli.py           命令行入口与子命令定义
  app.py / tui.py  交互界面与 curses 底座
scripts/
  build_pyz.py     打单文件发行版 dist/*.pyz
```

## 反馈与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)——提 issue 必须带系统版本、
包管理器版本和问题描述；PR 不接受无意义改动和界面改动。

## 许可

GPL-3.0，见 LICENSE。
