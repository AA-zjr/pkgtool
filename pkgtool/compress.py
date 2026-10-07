"""pkgtool.compress — 可能压缩的文本文件的统一读取。

合并原先散在三处的重复实现：
  deb_backend.open_any / deb_backend.read_log_lines /
  update_channels._read_any / apt_repo._read
按扩展名识别 .gz .xz .lzma .bz2，其余按纯文本读；一律 errors="replace"，
解析系统文件时遇到非法字节不该让整次采集失败。
"""
import bz2
import glob
import gzip
import lzma
import os

_OPENERS = {
    ".gz": lambda p: gzip.open(p, "rt", errors="replace"),
    ".xz": lambda p: lzma.open(p, "rt", errors="replace"),
    ".lzma": lambda p: lzma.open(p, "rt", errors="replace"),
    ".bz2": lambda p: bz2.open(p, "rt", errors="replace"),
}


def is_compressed(path):
    return os.path.splitext(path)[1].lower() in _OPENERS


def strip_compressed_suffix(path):
    """去掉压缩后缀 → 逻辑文件名（xxx_Packages.gz → xxx_Packages）。"""
    base, ext = os.path.splitext(path)
    return base if ext.lower() in _OPENERS else path


def open_text(path):
    """打开（可能压缩的）文本文件，返回文件对象；调用方负责 with。OSError 向上抛。"""
    opener = _OPENERS.get(os.path.splitext(path)[1].lower())
    return opener(path) if opener else open(path, "r", errors="replace")


def read_text(path):
    """整个读成字符串；文件不存在/读不了返回 None（不抛）。"""
    try:
        with open_text(path) as fh:
            return fh.read()
    except OSError:
        return None


def iter_lines(patterns):
    """按顺序遍历若干 glob 模式命中的所有文件的行，跳过读不到的文件。"""
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            try:
                with open_text(path) as fh:
                    yield from fh
            except OSError:
                continue
