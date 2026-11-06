"""端到端自测：索引 → 检索 → 问答 → 溯源。

用法：python tools/selftest.py
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from everyfind import db, qa, search  # noqa: E402
from everyfind.indexer import IndexManager  # noqa: E402

DEMO = ROOT / "演示资料"

QUESTIONS = [
    "验收报告、操作手册、问题清单分别由谁负责？",
    "差旅费的预算是多少？实际支出是多少？",
    "项目第三阶段是什么计划？第几周？",
    "春节放假放几天？",
    "验收测试用例通过率是多少？",
]

KEYWORDS = [
    "项目经理",
    "差旅费",
    "第三阶段",
    "春节",
    "验收",
]


def main() -> int:
    print("=" * 60)
    print("  EveryFind 端到端自测")
    print("=" * 60)

    db.init_db()
    mgr = IndexManager.instance()

    files = mgr.scan_root(str(DEMO), "folder")
    print(f"\n[1] 扫描到 {len(files)} 个文件")
    mgr.enqueue(files, "folder")
    t0 = time.time()
    mgr.wait_idle(timeout=300)
    print(f"    索引完成，用时 {time.time() - t0:.1f}s")

    counts = db.file_counts()
    print(f"    统计：{counts}")

    print("\n[2] 文件状态")
    for f in db.list_files():
        print(f"    - {f['name']}  [{f['status']}]  {f['chunk_count']} 块  {f['error'] or ''}")

    print("\n[3] 关键词检索")
    for kw in KEYWORDS:
        hits = search.search(kw, limit=3)
        print(f"    「{kw}」→ {len(hits)} 命中")
        for h in hits[:3]:
            print(f"        · {h['file_name']} · {h['loc_label']}  (score {h['score']})")
            print(f"          {h['snippet'][:70]}")

    print("\n[4] 问答")
    for q in QUESTIONS:
        res = qa.answer(q)
        print(f"\n    Q: {q}")
        print(f"    A: {res['answer'][:260]}")
        print(f"    模式={res['mode']}  引用={res['cited']}  涉及文件={len(res['files'])}")
        for c in res["citations"][:3]:
            print(f"        [{c['n']}] {c['file_name']} · {c['loc_label']}")
        assert res["citations"], "没有产生引用"

    print("\n[5] 溯源")
    res = qa.answer(QUESTIONS[1])
    c = res["citations"][0]
    d = qa.source_detail(c["chunk_id"], QUESTIONS[1])
    print(f"    文件：{d['file_name']}")
    print(f"    位置：{d['loc_label']}")
    if d.get("focus_cell"):
        print(f"    命中单元格：{d['focus_cell']['ref']} = {d['focus_cell']['value']}")
    print(f"    原文片段：{d['text'][:120]}")
    print(f"    高亮区间：{d['spans'][:6]}")

    res2 = qa.answer(QUESTIONS[2])
    d2 = qa.source_detail(res2["citations"][0]["chunk_id"], QUESTIONS[2])
    print(f"\n    PPT 溯源 → {d2['loc_label']}")
    print(f"    {d2['text'][:140]}")

    res3 = qa.answer(QUESTIONS[3])
    print(f"\n    OCR 问答 → {res3['answer'][:180]}")

    print("\n" + "=" * 60)
    print("  自测通过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
