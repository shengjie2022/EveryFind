"""本地 OCR：RapidOCR (ONNX) ，CPU 运行、离线可用。"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_engine: Any = None
_available: bool | None = None


def is_available() -> bool:
    global _available
    if _available is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401

            _available = True
        except Exception:
            _available = False
    return bool(_available)


def get_engine():
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
    return _engine


def ocr_image(src, min_confidence: float = 0.45) -> list[dict]:
    """识别图片文字，返回 [{text, score, box}]；box 为四点坐标。

    失败时**抛异常**，不要静默返回空列表：空列表会被上层当成
    「识别到了但图里没字」，用户完全看不出其实是 OCR 挂了。
    """
    if not is_available():
        raise RuntimeError("OCR 引擎不可用（缺少 rapidocr-onnxruntime 依赖）")

    import numpy as np
    from PIL import Image

    if isinstance(src, (str, bytes)) or hasattr(src, "__fspath__"):
        img = Image.open(src).convert("RGB")
    else:
        img = src
    arr = np.array(img)
    res, _ = get_engine()(arr)

    out: list[dict] = []
    for item in res or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except Exception:
            continue
        if not text or float(score) < min_confidence:
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append(
            {
                "text": str(text).strip(),
                "score": round(float(score), 4),
                "box": [min(xs), min(ys), max(xs), max(ys)],
            }
        )
    return out


def ocr_text(src, min_confidence: float = 0.45) -> str:
    lines = ocr_image(src, min_confidence)
    return "\n".join(l["text"] for l in lines)


def ocr_pdf_page(pdfium_doc, page_index: int, scale: float = 2.5,
                 min_confidence: float = 0.45) -> list[dict]:
    """渲染 PDF 单页并做 OCR。scale=2.5 约等于 180 DPI。

    渲染失败会抛异常，由调用方按页记录，不要静默跳过。
    """
    import numpy as np

    page = pdfium_doc[page_index]
    bitmap = page.render(scale=scale)
    img = bitmap.to_pil().convert("RGB")
    arr = np.array(img)
    return ocr_image(arr, min_confidence)
