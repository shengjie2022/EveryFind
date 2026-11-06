"""解析器调度：按扩展名选择解析器。"""
from __future__ import annotations

from pathlib import Path

from .base import Chunk, ParseError
from .docx_parser import DOCX_EXTS
from .image_parser import IMAGE_EXTS
from .pdf_parser import PDF_EXTS
from .pptx_parser import PPTX_EXTS
from .text_parser import TEXT_EXTS
from .xlsx_parser import XLSX_EXTS

LEGACY_EXTS = {".doc", ".ppt", ".xls"}

SUPPORTED_EXTS = sorted(
    DOCX_EXTS | XLSX_EXTS | PPTX_EXTS | PDF_EXTS | IMAGE_EXTS | TEXT_EXTS | LEGACY_EXTS
)

# 默认忽略的目录名
SKIP_DIRS = {
    "$recycle.bin", "system volume information", ".git", ".svn", "node_modules",
    "__pycache__", ".venv", "venv", "appdata", "windows", "program files",
    "program files (x86)", "$windows.~bt", "system volume", ".cache",
}


def ext_of(path: str | Path) -> str:
    return Path(path).suffix.lower()


def is_supported(path: str | Path) -> bool:
    return ext_of(path) in SUPPORTED_EXTS


def parse_file(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    """解析单个文件，返回 (内容块列表, 元信息)。"""
    opts = opts or {}
    path = Path(path)
    ext = ext_of(path)

    if ext in DOCX_EXTS:
        from .docx_parser import parse as p

        return p(path, opts)
    if ext in XLSX_EXTS:
        from .xlsx_parser import parse as p

        return p(path, opts)
    if ext in PPTX_EXTS:
        from .pptx_parser import parse as p

        return p(path, opts)
    if ext in PDF_EXTS:
        from .pdf_parser import parse as p

        return p(path, opts)
    if ext in IMAGE_EXTS:
        if not opts.get("ocr_enabled", True):
            raise ParseError("OCR 已关闭，跳过图片")
        from .image_parser import parse as p

        return p(path, opts)
    if ext in TEXT_EXTS:
        from .text_parser import parse as p

        return p(path, opts)
    if ext in LEGACY_EXTS:
        from ..legacy import parse_legacy

        return parse_legacy(path, opts)

    raise ParseError(f"暂不支持的文件类型：{ext or '无扩展名'}")


__all__ = [
    "Chunk",
    "ParseError",
    "SUPPORTED_EXTS",
    "SKIP_DIRS",
    "parse_file",
    "is_supported",
    "ext_of",
]
