"""内容块数据结构。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """文档中的一个可定位、可引用的内容单元。"""

    ord: int
    loc_type: str            # paragraph|cell|slide|page|line|image|table|header
    loc_label: str           # 人类可读定位，例如 "段落 6" / "Sheet1 · B4" / "第 3 页"
    text: str
    loc: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ord": self.ord,
            "loc_type": self.loc_type,
            "loc_label": self.loc_label,
            "text": self.text,
            "loc_json": json.dumps(self.loc, ensure_ascii=False),
        }


class ParseError(Exception):
    """解析失败（文件损坏、格式不支持等）。"""
