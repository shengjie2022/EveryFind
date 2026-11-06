"""Word (.docx) 解析：按段落 / 表格行 / 页眉页脚切块，保留原始顺序。"""
from __future__ import annotations

from pathlib import Path

from .base import Chunk, ParseError

DOCX_EXTS = {".docx", ".docm"}


def _iter_blocks(doc):
    """按文档流顺序产出 Paragraph 与 Table。"""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _col_letter(idx: int) -> str:
    letters = ""
    n = idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    try:
        from docx import Document
    except Exception as exc:  # pragma: no cover
        raise ParseError(f"python-docx 不可用: {exc}") from exc

    try:
        doc = Document(str(path))
    except Exception as exc:
        raise ParseError(f"无法打开 Word 文档: {exc}") from exc

    chunks: list[Chunk] = []
    ord_ = 0
    para_no = 0
    table_no = 0

    for block in _iter_blocks(doc):
        cls = type(block).__name__
        if cls == "Paragraph":
            para_no += 1
            text = (block.text or "").strip()
            if not text:
                continue
            style = ""
            try:
                style = block.style.name or ""
            except Exception:
                style = ""
            label = f"段落 {para_no}"
            if style and style.lower().startswith("heading"):
                label += f" · {style}"
            chunks.append(
                Chunk(
                    ord=ord_,
                    loc_type="paragraph",
                    loc_label=label,
                    text=text,
                    loc={"paragraph": para_no, "style": style, "kind": "paragraph"},
                )
            )
            ord_ += 1
        elif cls == "Table":
            table_no += 1
            for r_i, row in enumerate(block.rows, 1):
                cells = []
                for c_i, cell in enumerate(row.cells):
                    val = (cell.text or "").strip().replace("\n", " ")
                    cells.append({"col": _col_letter(c_i), "value": val})
                if not any(c["value"] for c in cells):
                    continue
                text = " | ".join(c["value"] for c in cells if c["value"])
                chunks.append(
                    Chunk(
                        ord=ord_,
                        loc_type="table",
                        loc_label=f"表格 {table_no} · 第 {r_i} 行",
                        text=text,
                        loc={
                            "kind": "table",
                            "table": table_no,
                            "row": r_i,
                            "cells": cells,
                        },
                    )
                )
                ord_ += 1

    # 页眉 / 页脚
    for s_i, section in enumerate(doc.sections, 1):
        for part_name, part in (("页眉", section.header), ("页脚", section.footer)):
            try:
                paras = part.paragraphs
            except Exception:
                continue
            for p_i, p in enumerate(paras, 1):
                text = (p.text or "").strip()
                if not text:
                    continue
                chunks.append(
                    Chunk(
                        ord=ord_,
                        loc_type="header",
                        loc_label=f"{part_name} · 第 {p_i} 段",
                        text=text,
                        loc={"kind": part_name, "section": s_i, "paragraph": p_i},
                    )
                )
                ord_ += 1

    # 文档属性
    try:
        cp = doc.core_properties
        props = [
            ("标题", cp.title),
            ("作者", cp.author),
            ("主题", cp.subject),
            ("关键词", cp.keywords),
            ("备注", cp.comments),
        ]
        for k, v in props:
            if v and str(v).strip():
                chunks.append(
                    Chunk(
                        ord=ord_,
                        loc_type="paragraph",
                        loc_label=f"文档属性 · {k}",
                        text=f"{k}：{str(v).strip()}",
                        loc={"kind": "property", "property": k},
                    )
                )
                ord_ += 1
    except Exception:
        pass

    meta = {
        "paragraphs": para_no,
        "tables": table_no,
        "title": (doc.core_properties.title or "") if hasattr(doc, "core_properties") else "",
    }
    return chunks, meta
