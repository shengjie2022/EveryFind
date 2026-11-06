"""图片解析：本地 OCR 识别图片里的文字（jpg/png/bmp/tif/webp 等）。"""
from __future__ import annotations

from pathlib import Path

from .base import Chunk, ParseError

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif"}


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    opts = opts or {}
    min_conf = float(opts.get("ocr_min_confidence", 0.45))

    from ..ocr import is_available, ocr_image

    if not is_available():
        raise ParseError("OCR 引擎不可用（缺少 rapidocr-onnxruntime 依赖）")

    try:
        lines = ocr_image(str(path), min_confidence=min_conf)
    except Exception as exc:
        raise ParseError(f"图片识别失败: {exc}") from exc

    if not lines:
        raise ParseError("图片中未识别到文字")

    text = "\n".join(l["text"] for l in lines)
    chunks = [
        Chunk(
            ord=0,
            loc_type="image",
            loc_label="图片全文",
            text=text,
            loc={
                "kind": "image",
                "source": "ocr",
                "lines": [{"text": l["text"], "score": l["score"], "box": l["box"]} for l in lines],
            },
        )
    ]
    meta = {"lines": len(lines), "ocr": True}
    return chunks, meta
