"""构建单文件发行版 dist/pkgtool.pyz（zipapp）。

零第三方依赖的直接兑现：产物是一个自带 shebang 的压缩 zip，接收方只要有
python3.10+ 就能直接运行，不需要 pip/venv。布局要求：包目录 + 根级
__main__.py（绝对导入）——直接对包目录做 zipapp 会把包内 __main__.py
顶到根上，相对导入随即失效（实测 ImportError）。

用法：python3 scripts/build_pyz.py
"""
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkgtool import __version__          # noqa: E402

OUT = ROOT / "dist" / f"pkgtool-{__version__}.pyz"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

MAIN = "import sys\nfrom pkgtool.cli import main\nsys.exit(main())\n"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        shutil.copytree(ROOT / "pkgtool", stage / "pkgtool", ignore=IGNORE)
        (stage / "__main__.py").write_text(MAIN, encoding="utf-8")
        OUT.parent.mkdir(exist_ok=True)
        if OUT.exists():
            OUT.unlink()
        zipapp.create_archive(stage, str(OUT), interpreter="/usr/bin/env python3",
                              compressed=True)
    print(f"已生成 {OUT}（{OUT.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
