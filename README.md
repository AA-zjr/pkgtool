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

## 反馈

提 issue 必须包含以下三项，缺一可能直接关闭：

1. 系统及版本号（发行版 + 版本）
2. 包管理器版本（`apt-get --version`、`snap version`、`flatpak --version`、
   `ll-cli --version`，装了哪个贴哪个）
3. 问题的描述与复现步骤；**有截图或终端输出更好**

## 贡献

PR 要求：

- 只改与问题直接相关的代码。顺手格式化、重命名变量、调整无关结构的
  改动会被拒绝
- **界面不接受 PR**：TUI 的视图、按键、显示逻辑不开放修改
- 能带上测试更好（`tests/`，标准库 unittest，跑法
  `python3 -m unittest discover -s tests`）

## 许可

GPL-3.0，见 LICENSE。
