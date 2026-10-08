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

或下载 Release 里的单文件 `pkgtool-*.pyz`：

```bash
chmod +x pkgtool-*.pyz
./pkgtool-*.pyz          # 之后的示例用 pkgtool 指代
```

## 使用

```
pkgtool                       交互界面
pkgtool list                  列出软件（--all 全层级，--loose 散落文件）
pkgtool info / run 名字        详情 / 启动
pkgtool search 关键词          搜 apt / Snap Store / flathub
pkgtool install -s snap 名字   安装；来源必填
pkgtool remove 名字            卸载，先预览再确认
pkgtool clean --list          磁盘回收清单
```

需要 root 的操作由 `sudo` 在终端提示密码，程序不经手。

已知限制：玲珑未接商店搜索；散落 AppImage 靠嗅探，可能漏报。

## 项目结构

```
pkgtool/
  config.py base.py        配置唯一来源 / 数据模型与后端契约
  apt/  backends/          deb 深层数据 / 五种格式采集器
  inventory.py             采集编排，唯一数据入口
  classify.py              卸载风险分类与显示层级
  remove clean upgrade launch catalog   卸载/回收/升级/启动/三源搜索
  report.py cli.py app.py tui.py        输出 / 命令行 / 终端界面
scripts/build_pyz.py       打单文件发行版
```

反馈与贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)：issue 必须带系统版本、
包管理器版本和问题描述；PR 不接受无意义改动和界面改动。

## 许可

GPL-3.0，见 LICENSE。
