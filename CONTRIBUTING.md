# 贡献指南

## 提 issue

必须包含以下三项，缺一可能被直接关闭：

1. **系统及版本号**：发行版和版本（如 Ubuntu 26.04 LTS / Debian 12 / deepin 23）
2. **包管理器版本**：装了哪个贴哪个
   ```bash
   apt-get --version
   dpkg --version
   snap version
   flatpak --version
   ll-cli --version
   ```
3. **问题描述**：做了什么、期望什么、实际发生了什么，附复现步骤。
   **有截图或终端原文输出更好**，纯转述经常说不清关键细节。

找不到规律的问题，把 `pkgtool config` 的输出一并贴上。

## 提 PR

- **一个 PR 只解决一个问题**，只改与它直接相关的代码
- 顺手格式化、重命名变量、调整无关结构、换写法这类**无意义改动会被拒绝**
- **界面不接受 PR**：TUI 的视图划分、按键绑定、显示逻辑不开放修改，
  相关建议请开 issue 讨论
- 提交信息用中文一句话说清动机，不要只写"修复"、"更新"
- 提交即表示同意代码以 GPL-3.0 授权发布

## 本地运行

```bash
git clone <仓库地址>
python3 -m pkgtool list          # 确认能跑
python3 -m pkgtool               # 交互界面
python3 scripts/build_pyz.py     # 打单文件发行版
```

要求 Python >= 3.10，无其他依赖。
