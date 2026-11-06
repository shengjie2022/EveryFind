"""把源码打包成 zip（不含 build/dist/data 与索引库）。"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "EveryFind-源码.zip"
SKIP_DIRS = {"build", "dist", "data", "__pycache__", ".git", ".venv", ".venv312", "local-secrets", ".review", "logs"}
SKIP_EXT = {".pyc", ".db", ".db-shm", ".db-wal", ".exe"}

OUT.parent.mkdir(parents=True, exist_ok=True)
count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            if fn.startswith(".env") or fn == ".DS_Store" or p.suffix.lower() in SKIP_EXT:
                continue
            if p.resolve() == OUT:
                continue
            z.write(p, p.relative_to(ROOT))
            count += 1

size = OUT.stat().st_size / 1024
print(f"已打包 {count} 个文件 -> {OUT}  ({size:.0f} KB)")
