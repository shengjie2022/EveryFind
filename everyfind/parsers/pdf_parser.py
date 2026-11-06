"""PDF 解析：pdfplumber 取文本与表格，扫描页用本地 OCR 兜底。"""
from __future__ import annotations

from pathlib import Path

from .base import Chunk, ParseError

PDF_EXTS = {".pdf"}


def _flatten_tables(tables) -> list[str]:
    out: list[str] = []
    for tb in tables or []:
        for row in tb:
            vals = [(c or "").replace("\n", " ").strip() for c in row]
            if any(vals):
                out.append(" | ".join(v for v in vals if v))
    return out


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    opts = opts or {}
    ocr_enabled = bool(opts.get("ocr_enabled", True))
    ocr_pdf = bool(opts.get("ocr_scanned_pdf", True))
    min_conf = float(opts.get("ocr_min_confidence", 0.45))

    chunks: list[Chunk] = []
    ord_ = 0
    page_count = 0

    # ---------- 文本层 ----------
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            blank_pages: list[int] = []
            for p_i, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                try:
                    tables = page.extract_tables()
                except Exception:
                    tables = []
                trows = _flatten_tables(tables)
                body = (text.strip() + ("\n" + "\n".join(trows) if trows else "")).strip()

                if not body:
                    blank_pages.append(p_i)
                    continue
                for offset in range(0, len(body), 6000):
                    chunks.append(
                        Chunk(
                            ord=ord_, loc_type="page", loc_label=f"第 {p_i} 页",
                            text=body[offset:offset + 6000],
                            loc={"kind": "page", "page": p_i, "source": "text", "offset": offset},
                        )
                    )
                    ord_ += 1
    except Exception as exc:
        raise ParseError(f"PDF 解析失败: {exc}") from exc

    # ---------- 扫描页 OCR 兜底 ----------
    ocr_errors: list[str] = []
    if ocr_enabled and ocr_pdf and blank_pages:
        extra, ocr_errors = _ocr_blank_pages(path, blank_pages, ord_, min_conf)
        chunks.extend(extra)
        ord_ = len(chunks)

    meta = {"pages": page_count, "scanned_pages": len(blank_pages) if ocr_enabled else 0}
    if ocr_errors:
        # OCR 失败要能被体检报告看到，否则用户只看到「1 块」却不知道是空的
        meta["ocr_errors"] = ocr_errors[:10]
    return chunks, meta


def _ocr_blank_pages(path: Path, pages: list[int], ord_: int,
                     min_conf: float) -> tuple[list[Chunk], list[str]]:
    out: list[Chunk] = []
    errors: list[str] = []
    try:
        import pypdfium2 as pdfium

        from ..ocr import ocr_pdf_page
    except Exception as exc:
        return out, [f"OCR 组件不可用：{exc}"]

    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:
        return out, [f"PDF 渲染失败：{exc}"]

    try:
        for p_i in pages:
            try:
                lines = ocr_pdf_page(doc, p_i - 1, min_confidence=min_conf)
            except Exception as exc:
                errors.append(f"第 {p_i} 页 OCR 失败：{exc}")
                continue
            if not lines:
                continue
            text = "\n".join(l["text"] for l in lines)
            out.append(
                Chunk(
                    ord=ord_,
                    loc_type="page",
                    loc_label=f"第 {p_i} 页（OCR 识别）",
                    text=text,
                    loc={
                        "kind": "page",
                        "page": p_i,
                        "source": "ocr",
                        "lines": [
                            {"text": l["text"], "score": l["score"], "box": l["box"]} for l in lines
                        ],
                    },
                )
            )
            ord_ += 1
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out, errors
