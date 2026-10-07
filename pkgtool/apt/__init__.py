"""pkgtool.apt — deb/apt 的数据源与写操作。

子模块：
  version  Debian 版本号比较（dpkg verrev，无 python-debian 依赖）
  lists    /var/lib/apt/lists 的唯一解析器（仓库索引 + 源标签 + section/priority）
  logs     dpkg.log / apt history.log 时间线（安装通道判定的证据来源）
  dpkg     /var/lib/dpkg 状态：status、.list 清单、extended_states、apt-mark
  actions  写操作：apt-get download / 特权执行
"""
