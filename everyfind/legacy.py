"""旧版 Office 格式 (.doc/.ppt/.xls) 支持。

Windows 上若安装了 Microsoft Office，则通过 COM 自动转换为新版格式后解析；
未安装 Office 时给出明确提示，不影响其它格式。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .parsers.base import Chunk, ParseError


def office_available() -> bool:
    try:
        import win32com.client  # noqa: F401
    except Exception:
        return False
    return os.name == "nt"


def _convert(path: Path, fmt: str, out_ext: str) -> Path:
    """用 Office COM 把旧格式转成新版。fmt: 'word'|'excel'|'powerpoint'"""
    import win32com.client

    tmpdir = Path(tempfile.mkdtemp(prefix="everyfind_conv_"))
    out_path = tmpdir / (path.stem + out_ext)
    app = None
    try:
        app = win32com.client.DispatchEx(fmt)
        doc = app.Documents.Open(str(path), ReadOnly=True)
        doc.SaveAs(str(out_path), FileFormat=16)  # wdFormatDocumentDefault / xlDefault
        doc.Close(SaveChanges=0)
    except Exception as exc:
        raise ParseError(f"旧版格式转换失败（需要本机安装 Microsoft Office）：{exc}") from exc
    finally:
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
    if not out_path.exists():
        raise ParseError("旧版格式转换失败：未生成输出文件")
    return out_path


def parse_legacy(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    from .parsers import parse_file

    ext = path.suffix.lower()
    if not office_available():
        raise ParseError(
            f"{ext} 是旧版 Office 格式，需要本机安装 Microsoft Office 才能解析；"
            "也可以先用 Office 另存为新版格式再导入。"
        )

    try:
        if ext == ".doc":
            converted = _convert(path, "Word.Application", ".docx")
            chunks, meta = parse_file(converted, opts)
        elif ext == ".xls":
            converted = _convert(path, "Excel.Application", ".xlsx")
            chunks, meta = parse_file(converted, opts)
        elif ext == ".ppt":
            converted = _convert(path, "PowerPoint.Application", ".pptx")
            chunks, meta = parse_file(converted, opts)
        else:
            raise ParseError(f"不支持的旧版格式：{ext}")
    finally:
        pass

    meta = dict(meta)
    meta["converted_from"] = ext
    return chunks, meta
