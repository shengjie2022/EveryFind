"""PowerPoint (.pptx) 解析：按幻灯片切块，标题与表格都保留。"""
from __future__ import annotations

from pathlib import Path

from .base import Chunk, ParseError

PPTX_EXTS = {".pptx", ".pptm"}


def _shape_texts(shape) -> list[str]:
    out: list[str] = []
    try:
        if getattr(shape, "has_text_frame", False):
            for p in shape.text_frame.paragraphs:
                t = "".join(r.text for r in p.runs).strip()
                if not t:
                    t = (p.text or "").strip()
                if t:
                    out.append(t)
    except Exception:
        pass
    try:
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                vals = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                if any(vals):
                    out.append(" | ".join(v for v in vals if v))
    except Exception:
        pass
    try:
        if shape.shape_type == 6:  # GROUP
            for sub in shape.shapes:
                out.extend(_shape_texts(sub))
    except Exception:
        pass
    return out


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    try:
        from pptx import Presentation
    except Exception as exc:  # pragma: no cover
        raise ParseError(f"python-pptx 不可用: {exc}") from exc

    try:
        prs = Presentation(str(path))
    except Exception as exc:
        raise ParseError(f"无法打开 PPT: {exc}") from exc

    chunks: list[Chunk] = []
    ord_ = 0

    for idx, slide in enumerate(prs.slides, 1):
        title = ""
        try:
            if slide.shapes.title is not None:
                title = (slide.shapes.title.text or "").strip()
        except Exception:
            pass

        texts: list[str] = []
        for shape in slide.shapes:
            for t in _shape_texts(shape):
                if t and t != title:
                    texts.append(t)

        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:
            pass

        body = "\n".join(texts)
        label = f"第 {idx} 张幻灯片"
        if title:
            label += f" · {title.splitlines()[0][:40]}"

        chunks.append(
            Chunk(
                ord=ord_,
                loc_type="slide",
                loc_label=label,
                text=(title + "\n" + body).strip() if title else body,
                loc={"kind": "slide", "slide": idx, "title": title},
            )
        )
        ord_ += 1

        if notes:
            chunks.append(
                Chunk(
                    ord=ord_,
                    loc_type="slide",
                    loc_label=f"第 {idx} 张幻灯片 · 备注",
                    text=notes,
                    loc={"kind": "slide_notes", "slide": idx, "title": title},
                )
            )
            ord_ += 1

    slide_count = len(prs.slides._sldIdLst)
    meta = {"slides": slide_count}
    return chunks, meta
