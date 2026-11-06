"""打包成桌面程序：Windows 生成免安装目录，macOS 生成 .app。

用法：
    python tools/build.py            # 按当前系统打包
    python tools/build.py --clean    # 先清理 build/dist
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 控制台默认 GBK，打印 ✗ / ✓ 之类的符号会抛 UnicodeEncodeError，
# 反而把真正的报错盖掉，这里统一切到 UTF-8 并容错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_NAME = "EveryFind"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="打包前清理 build/ 与 dist/")
    ap.add_argument("--onefile", action="store_true", help="打包成单文件 exe（启动较慢）")
    args = ap.parse_args()

    if sys.prefix == sys.base_prefix:
        print('请使用本项目 .venv 内的 Python 运行打包工具，避免修改全局环境。')
        return 1
    if shutil.which("pyinstaller") is None:
        try:
            import PyInstaller  # noqa: F401
        except Exception:
            print("· 未安装 PyInstaller，正在安装…")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "pyinstaller"], check=True
            )

    if args.clean:
        for d in ("build", "dist"):
            p = ROOT / d
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
        "--hidden-import", "everyfind.server",
        "--hidden-import", "rapidocr_onnxruntime",
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-all", "jieba",
        "--collect-all", "pdfplumber",
        "--collect-all", "pypdfium2",
        "--collect-all", "pptx",
        "--collect-all", "docx",
        "--collect-all", "openpyxl",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "torch",
        "--exclude-module", "notebook",
        "--exclude-module", "IPython",
        "--exclude-module", "pytest",
        # 注意：setuptools / pkg_resources 不能排除，jieba 启动时要用它定位词典
        "--hidden-import", "pkg_resources",
        "--hidden-import", "jaraco.text",
        # 下面这些是被 OCR 依赖树顺带拖进来的，用不到，去掉能省 500MB+
        "--exclude-module", "jax",
        "--exclude-module", "jaxlib",
        "--exclude-module", "llvmlite",
        "--exclude-module", "scipy",
        "--exclude-module", "pandas",
        "--exclude-module", "botocore",
        "--exclude-module", "boto3",
        "--exclude-module", "tensorflow",
        "--exclude-module", "sklearn",
        "--exclude-module", "numba",
        # cryptography 不能排除：pypdf 解析加密 PDF 时要用
        "--hidden-import", "cryptography",
        "--hidden-import", "pypdf._encryption",
    ]
    if args.onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")
    cmd.append(str(ROOT / "run.py"))

    print("· 执行：", " ".join(cmd[:12]), "…")
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("✗ 打包失败")
        return result.returncode

    dist = ROOT / "dist"
    print("\n打包完成，产物：")
    for p in sorted(dist.iterdir()):
        print("  ·", p)
    print("\n提示：把 dist/" + APP_NAME + ("/EveryFind.exe" if os.name == "nt" else "/EveryFind")
          + " 整个拷走即可使用；首次运行会在同级目录生成 data/。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
