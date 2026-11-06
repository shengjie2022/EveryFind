"""Excel (.xlsx/.xlsm) 解析：工作表 + 行号 + 单元格坐标，保留表头上下文。"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from .base import Chunk, ParseError

XLSX_EXTS = {".xlsx", ".xlsm"}

# 这些表头只是"行标签"列，前缀拼接没有意义
_GENERIC_HEADERS = {"项目", "名称", "类别", "类型", "序号", "项目名称", "科目", "说明"}


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, _dt.datetime) else v.isoformat()
    return str(v).strip()


def _is_header_like(values: list[str]) -> bool:
    """判断一行是否像表头：多列、且绝大多数是文本。"""
    vals = [v for v in values if v]
    if len(vals) < 2:
        return False
    texty = 0
    for v in vals:
        try:
            float(v.replace(",", ""))
        except ValueError:
            texty += 1
    return texty >= max(2, int(len(vals) * 0.6))


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise ParseError(f"openpyxl 不可用: {exc}") from exc

    wb = None
    formulas = None
    try:
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=False)
        formulas = openpyxl.load_workbook(str(path), data_only=False, read_only=False)
    except Exception as exc:
        if wb is not None:
            wb.close()
        raise ParseError(f"无法打开 Excel: {exc}") from exc

    chunks: list[Chunk] = []
    ord_ = 0
    sheet_meta: list[dict] = []

    try:
        for ws in wb.worksheets:
            rows_data: list[tuple[int, list[dict]]] = []
            for row in ws.iter_rows():
                cells = []
                for c in row:
                    value = c.value
                    formula_cell = formulas[ws.title][c.coordinate]
                    if value is None and formula_cell.data_type == 'f':
                        value = '未计算公式：' + str(formula_cell.value)
                    val = _fmt(value)
                    if val:
                        cells.append({"ref": c.coordinate, "row": c.row, "col": c.column, "value": val})
                if cells:
                    rows_data.append((row[0].row, cells))

            headers: list[str] = []
            header_row = 0
            if rows_data:
                # 表头 = 前 5 行里「非空单元格最多」且以文本为主的那一行
                best_cells: list[dict] = []
                best_idx = 0
                for i, (_r, cs) in enumerate(rows_data[:5]):
                    if len(cs) > len(best_cells):
                        best_cells, best_idx = cs, i
                if len(best_cells) >= 2 and _is_header_like([c["value"] for c in best_cells]):
                    max_col = max(c["col"] for c in best_cells)
                    headers = [""] * max_col
                    for c in best_cells:
                        headers[c["col"] - 1] = c["value"]
                    header_row = rows_data[best_idx][0]

            sheet_meta.append(
                {
                    "name": ws.title,
                    "rows": ws.max_row or 0,
                    "cols": ws.max_column or 0,
                    "header_row": header_row,
                }
            )

            chunks.append(
                Chunk(
                    ord=ord_,
                    loc_type="cell",
                    loc_label=f"工作表「{ws.title}」",
                    text=f"工作表名称：{ws.title}（{ws.max_row or 0} 行 × {ws.max_column or 0} 列）",
                    loc={"kind": "sheet", "sheet": ws.title},
                )
            )
            ord_ += 1

            for r_i, cells in rows_data:
                values = [c["value"] for c in cells]
                # 用表头补全上下文，显著提升问答命中率
                parts = []
                cell_meta = []
                for idx, c in enumerate(cells):
                    header = ""
                    if headers and r_i > header_row and c["col"] - 1 < len(headers):
                        header = headers[c["col"] - 1]
                    if header and header != c["value"] and header not in _GENERIC_HEADERS:
                        parts.append(f"{header}：{c['value']}")
                    else:
                        parts.append(c["value"])
                    cell_meta.append(
                        {"ref": c["ref"], "header": header, "value": c["value"], "row": r_i}
                    )
                text = " | ".join(p for p in parts if p)
                chunks.append(
                    Chunk(
                        ord=ord_,
                        loc_type="cell",
                        loc_label=f"{ws.title} · 第 {r_i} 行",
                        text=text,
                        loc={
                            "kind": "sheet_row",
                            "sheet": ws.title,
                            "row": r_i,
                            "cells": cell_meta,
                            "headers": headers,
                        },
                    )
                )
                ord_ += 1
    finally:
        try:
            wb.close()
            formulas.close()
        except Exception:
            pass

    meta = {"sheets": sheet_meta, "sheet_count": len(sheet_meta)}
    return chunks, meta
