"""表格聚合计算：求和 / 计数 / 极值 / 平均 / 分组。

数据来源是**已经索引好的表格行块** —— Excel 解析时每个数据行都会留下
`loc_json.cells`，里面带着表头名、单元格坐标和原始值。所以这里不需要
重新打开文件、也不需要额外的索引表，直接从 chunks 里读结构化数据算。

设计上刻意保守：算错比不算更糟。所以
- 意图必须明确（问题里出现聚合词）
- 目标列必须能唯一确定（问题里的词能对上表头，或整张表只有一个数值列）
- **合计行必须排除**，否则「合计」那一行会被再算一遍，直接双倍计数
- **不做跨文件求和**：不同文件里的「预算」不是同一笔钱，
  硬加在一起等于把副本算两遍。多个文件都有这一列时，按文件分别列出。

算不出来就返回 None，交回原来的检索流程，绝不硬凑。
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

from . import db, textutil

# ------------------------------------------------------------------ 意图

# 计数：必须带量词，否则「预算是多少」会被误当成计数
_RE_COUNT = re.compile(r"(?:多少|几)\s*(?:个|项|条|份|张|页|人|件|台|种|款|家|行|列|块|册)|数量")
_RE_SUM = re.compile(r"合计|总共|总和|总计|总额|总数|加起来|加总|求和|累计|总金额|总费用|总成本|总预算")
_RE_MAX = re.compile(r"最大|最高|最多|最贵|峰值|最突出|排第一")
_RE_MIN = re.compile(r"最小|最低|最少|最便宜|垫底")
_RE_AVG = re.compile(r"平均|均值|人均|平均每")

# 出现这些词，说明用户想要「分组看」
_RE_GROUP_HINT = re.compile(r"按|每个|各|分组|分类|分别列|逐个")

# 合计行：这些行的数值不能参与聚合（否则双倍计数）
# 写法很多：「合计」「合计：」「共计」「本年合计」「合 计」「Total」「小计」「合计（万元）」
# 所以先去掉空白、冒号和括号里的单位说明，再比关键词。
_RE_TOTAL_ROW = re.compile(
    r"^(?:本年|本年度|本季度|本月的?|年度|季度)?"
    r"(?:合计|共计|小计|总计|总和|总额|累计|总共|总值|平均|平均值|平均数|"
    r"total|subtotal|sum|avg|average)"
    r"(?:数|额|值)?$",
    re.I,
)

# 数值解析时排掉的形态：日期
_RE_DATE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日|年|月|日")
# 「第3阶段」「第 3 季度」这类带序号的文本：里面的数字是编号，不是数量
_RE_ORDINAL = re.compile(r"第\s*\d")
_CURRENCY = "¥￥$€£元人民币块"

MAX_CITATIONS = 6
# 单个文件最多参与多少行计算。超了就整个放弃 —— 少算一半的数比不算更糟。
MAX_ROWS = 100000
# 表格行解析结果的缓存（按文件）。重新索引后靠 stamp 失效。
_ROWS_CACHE: dict[int, tuple[str, list[dict]]] = {}
_ROWS_CACHE_LOCK = threading.Lock()
_ROWS_CACHE_MAX = 32


def _file_stamp(conn, file_id: int) -> str:
    """文件版本的指纹。重新索引后 mtime/块数会变，缓存自然失效。"""
    r = conn.execute(
        "SELECT mtime, status, chunk_count FROM files WHERE id=?", (file_id,)
    ).fetchone()
    if not r:
        return ""
    return f"{r['mtime']}|{r['status']}|{r['chunk_count']}"


def _clean(s: str) -> str:
    return re.sub(r"[\s　（）()【】\[\]：:，,。.、/／\-—_“”\"']+", "", str(s or ""))


def _stem(header: str) -> str:
    """表头去掉括号里的单位说明。

    「预算（元）」→ 「预算」。带单位的表头用全名去匹配问题会很难对上，
    去掉括号部分后「预算合计是多少」就能直接命中「预算」。
    """
    h = re.sub(r"[（(][^）)]*[）)]", "", str(header or ""))
    return _clean(h) or _clean(header)


def to_num(value: Any) -> float | None:
    """把单元格里的值转成数字；不是数字就返回 None。

    「48,000」「￥32,000元」「1.5万」「3.5 万元」「(1,000)」都能认；
    「2026-01-01」「第三阶段」「张岚」这种一律不当数字。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value or "").strip()
    if not s or _RE_DATE.search(s) or _RE_ORDINAL.search(s):
        return None
    # 会计写法：(1,000) 表示负数
    neg = False
    m0 = re.match(r"^\((.*)\)$", s)
    if m0:
        neg = True
        s = m0.group(1).strip()
    s = s.replace(",", "").replace("，", "")
    # 数字里的千分位空格：「48 000」是一个数，不是两个
    s = re.sub(r"(?<=\d)[ \u3000]+(?=\d)", "", s)
    s = re.sub(r"^[%s]" % re.escape(_CURRENCY), "", s)
    s = re.sub(r"[%s%%]+$" % re.escape(_CURRENCY), "", s)
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    # 数字后面跟着「万/亿」要换算，否则「1.5万」会算成 1.5。
    # 尾巴要先去掉空格：「3.5 万元」的尾巴是 " 万元"，不 strip 会漏掉换算。
    num = float(m.group(0))
    tail = s[m.end():].strip()
    if tail.startswith("亿"):
        num *= 1e8
    elif tail.startswith("万"):
        num *= 1e4
    if neg:
        num = -num
    return num


def fmt_num(v: float) -> str:
    """人看着舒服的数字写法：48000 → 48,000；248000.5 → 248,000.5"""
    if abs(v) < 5e-5:
        return "0"          # 避免出现 "-0"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ------------------------------------------------------------------ 取数

def _is_total_label(value: str) -> bool:
    """这个单元格是不是合计标记。

    判定前先把「合计：」「本年合计」「合计（万元）」「合 计」归一化，
    只比「合计」两个字会漏掉一大半实际写法，漏掉就等于把合计行又算了一遍。
    """
    s = re.sub(r"[（(][^）)]*[）)]", "", str(value or "").strip())
    s = re.sub(r"[\s　:：]", "", s)
    return bool(s) and bool(_RE_TOTAL_ROW.match(s))


def _is_total_row(cells: list[dict]) -> bool:
    """这一行是不是「合计」行（任一单元格是合计标记就算）。"""
    for c in cells:
        if _is_total_label(str(c.get("value") or "")):
            return True
    return False


def _rows_of_file(conn, file_id: int) -> list[dict] | None:
    """取一个文件里已解析的表格行（按文件缓存）。

    以前每次提问都重扫一遍全表 + 逐行 json.loads —— 两万行的表要 3 秒多，
    而用户问的往往只是几行。解析结果按文件缓存，重新索引后自动失效。

    返回 None 表示「这个文件的表太大，算不全」→ 整个计算放弃，
    宁可交回检索流程，也不能给一个少算了一半的数。
    """
    stamp = _file_stamp(conn, file_id)
    with _ROWS_CACHE_LOCK:
        hit = _ROWS_CACHE.get(file_id)
        if hit is not None and hit[0] == stamp:
            return hit[1]

    out: list[dict] = []
    rows = conn.execute(
        """SELECT c.id, c.file_id, c.loc_label, c.loc_json, c.text,
                  f.name AS file_name, f.path AS file_path, f.ext AS file_ext,
                  f.rel_dir AS file_rel_dir
           FROM chunks c JOIN files f ON f.id = c.file_id
           WHERE c.file_id = ? AND c.loc_type='cell' AND c.loc_json LIKE '%sheet_row%'
           LIMIT ?""",
        (file_id, MAX_ROWS + 1),
    ).fetchall()
    if len(rows) > MAX_ROWS:
        return None
    for r in rows:
        try:
            loc = json.loads(r["loc_json"] or "{}")
        except Exception:
            continue
        cells = loc.get("cells") or []
        if not cells:
            continue
        # 合计行必须排除：它本身就是所有分项的和，再加一次就双倍了
        if _is_total_row(cells):
            continue
        out.append(
            {
                "chunk_id": int(r["id"]),
                "file_id": int(r["file_id"]),
                "file_name": r["file_name"],
                "file_path": r["file_path"],
                "file_ext": r["file_ext"],
                "rel_dir": r["file_rel_dir"] or "",
                "loc_label": r["loc_label"],
                "text": r["text"] or "",
                "sheet": loc.get("sheet") or "",
                "row": loc.get("row") or 0,
                "cells": cells,
            }
        )
    with _ROWS_CACHE_LOCK:
        if len(_ROWS_CACHE) >= _ROWS_CACHE_MAX:
            _ROWS_CACHE.clear()
        _ROWS_CACHE[file_id] = (stamp, out)
    return out


def _load_rows(file_id: int | None, dir_prefix: str | None = None) -> list[dict]:
    """从已索引的表格行块里还原结构化行（已排除合计行）。"""
    conn = db.connect()
    # 注意表别名：db.dir_scope 生成的条件里用的是「f.rel_dir」，
    # 这里必须写成 FROM files f —— 写成 FROM files 就会报 no such column: f.rel_dir
    sql = "SELECT f.id FROM files f WHERE f.status='ok'"
    args: list[Any] = []
    if file_id is not None:
        sql += " AND f.id=?"
        args.append(file_id)
    dir_clause, dir_args = db.dir_scope(dir_prefix or "")
    if dir_clause:
        sql += dir_clause
        args.extend(dir_args)
    out: list[dict] = []
    for r in conn.execute(sql, args):
        rows = _rows_of_file(conn, int(r["id"]))
        if rows is None:
            # 这个文件的表太大，算不全 —— 整个计算放弃，不给少算的数
            return []
        out.extend(rows)
    return out


def _numeric_columns(rows: list[dict]) -> dict[str, list[dict]]:
    """{表头: [该列在这一行的取值]}，只保留「确实有多个数字」的列。

    表头用**原始写法**做键（不去括号）：「预算（元）」和「预算（万元）」是两个列，
    去括号后都变成「预算」，会被合并成一列，两列的数直接加到一起 ——
    「100 元 + 50 万元」会算成 150，比算错更糟。
    """
    cols: dict[str, list[dict]] = {}
    for row in rows:
        for c in row["cells"]:
            header = str(c.get("header") or "").strip()
            if not header:
                continue
            num = to_num(c.get("value"))
            cols.setdefault(header, []).append(
                {
                    "num": num,
                    "value": c.get("value"),
                    "ref": c.get("ref"),
                    "row": row,
                }
            )
    # 只留至少两行是数字的列 —— 一行是数字的列不是「数值列」，多半是编号或日期
    return {
        h: items
        for h, items in cols.items()
        if sum(1 for i in items if i["num"] is not None) >= 2
    }


def _header_score(header: str, question: str) -> float:
    """表头和问题有多像。0~1。"""
    h = _stem(header)
    if len(h) < 2:
        return 0.0
    if h in question:
        return 1.0
    hs = set(textutil.char_bigrams(h))
    if not hs:
        return 0.0
    qs = set(textutil.char_bigrams(question))
    return len(hs & qs) / len(hs)


def _unit_of(header: str) -> str:
    """表头括号里写的单位：「预算（万元）」→ 万元；没写就是空串。

    单位是「能不能把两列的数加在一起」的判据：
    「预算（元）」和「预算（万元）」名字一样但单位不同，数值绝不能相加。
    """
    m = re.search(r"[（(]([^）)]*)[）)]", str(header or ""))
    return _clean(m.group(1)) if m else ""


def _column_groups(num_cols: dict[str, list[dict]]) -> list[tuple[str, str, list[str]]]:
    """把数值列按（去单位后的名字, 单位）分组。

    返回 [(名字, 单位, [原始表头...]), ...]。
    同名同单位的表头（不同文件里同样写法）合成一组，可以合并；
    同名不同单位的分成两组 —— 绝不混在一起加。
    """
    buckets: dict[tuple[str, str], list[str]] = {}
    for h in num_cols:
        buckets.setdefault((_stem(h), _unit_of(h)), []).append(h)
    return [(stem, unit, hs) for (stem, unit), hs in buckets.items()]


def _pick_column_groups(question: str, num_cols: dict[str, list[dict]]
                        ) -> list[tuple[str, str, list[str]]]:
    """挑出要做聚合的列组。

    优先用问题里点名的列；问题没点名、但整张表只有一个数值列组时也接受。
    两类列都拿不准就放弃 —— 宁可不算，也不能算到别的列上去。

    同名不同单位的组分数必然相同：它们**分别算、分别列**，不合并，
    这样既不会把「100 元 + 50 万元」加在一起，又能把两个口径都告诉用户。
    """
    groups = _column_groups(num_cols)
    if not groups:
        return []
    scored = sorted(
        ((_header_score(stem, question), stem, unit, hs)
         for stem, unit, hs in groups),
        key=lambda x: -x[0],
    )
    best_score, best_stem = scored[0][0], scored[0][1]
    same = [g for g in scored if g[1] == best_stem]
    rivals = [g for g in scored if g[1] != best_stem]
    if rivals:
        # 别的列名也咬合得不错 —— 分不清要哪一列，宁可不算
        if best_score < 0.5 or best_score < rivals[0][0] + 0.1:
            return []
    elif len(scored) > 1 and best_score < 0.5:
        return []
    return [(stem, unit, hs) for _, stem, unit, hs in same]


def _group_column(question: str, rows: list[dict], exclude: str) -> str | None:
    """找出分组列。

    不去抠问题里的「按 X」，而是拿所有文本列去跟问题比对 ——
    「按类别统计预算合计」里「类别」二字直接出现在问题中，比正则抠名字可靠。

    排除目标列时按**去单位后的名字**比：「预算（元）」的目标列，
    不该让同名的「预算（万元）」来当分组列。
    """
    if not _RE_GROUP_HINT.search(question):
        return None
    ex_stem = _stem(exclude)
    headers: set[str] = set()
    for row in rows:
        for c in row["cells"]:
            h = str(c.get("header") or "").strip()
            if h and _stem(h) != ex_stem:
                headers.add(h)
    best, best_score = None, 0.0
    for h in headers:
        s = _header_score(h, question)
        if s > best_score:
            best, best_score = h, s
    return best if best_score >= 0.5 else None


def _named_file(question: str, dir_prefix: str | None = None) -> int | None:
    """问题里点名了哪个文件（唯一命中才算）。

    必须和检索口径一致地带上目录限定：否则限定到某个目录时，
    仍会拿目录外的同名文件去锁定，行数对不上就直接算不出来。
    """
    conn = db.connect()
    q = question.lower()
    clause, args = db.dir_scope(dir_prefix or "")
    hits: list[int] = []
    for r in conn.execute(
        "SELECT f.id, f.name FROM files f WHERE f.status='ok'" + clause, args
    ):
        stem = (r["name"] or "").lower().rsplit(".", 1)[0]
        if len(stem) >= 2 and stem in q:
            hits.append(int(r["id"]))
    return hits[0] if len(hits) == 1 else None


def _hit_from_row(row: dict, query: str) -> dict:
    """把一行还原成和检索结果同构的 hit，供引用与溯源使用。"""
    return {
        "chunk_id": row["chunk_id"],
        "file_id": row["file_id"],
        "file_name": row["file_name"],
        "file_path": row["file_path"],
        "file_ext": row["file_ext"],
        "file_rel_dir": row.get("rel_dir") or "",
        "ord": 0,
        "loc_type": "cell",
        "loc_label": row["loc_label"],
        "loc_json": json.dumps(
            {"kind": "sheet_row", "sheet": row["sheet"], "row": row["row"]}, ensure_ascii=False
        ),
        "text": row["text"],
        "score": 1.0,
        "snippet": textutil.snippet(row["text"], query),
        "spans": textutil.highlight_spans(row["text"], query),
    }


def _label(row: dict) -> str:
    d = row.get("rel_dir") or ""
    return (d + "/" if d else "") + row["file_name"]


# ------------------------------------------------------------------ 主流程

def detect_op(question: str) -> str:
    """识别聚合动作。返回 sum/count/max/min/avg 或空串。"""
    q = question or ""
    # 同时问「最小和最大」没法用单个聚合表达 —— 宁可不算，
    # 也不能只给其中一个、让用户以为那就是全部
    has_max = bool(_RE_MAX.search(q))
    has_min = bool(_RE_MIN.search(q))
    if has_max and has_min:
        return ""
    if has_max:
        return "max"
    if has_min:
        return "min"
    if _RE_AVG.search(q):
        return "avg"
    if _RE_COUNT.search(q):
        return "count"
    if _RE_SUM.search(q):
        return "sum"
    return ""


def _aggregate(op: str, values: list[dict]) -> tuple[float, dict | None]:
    """在数值集合上做聚合，返回 (结果值, 极值对应的条目)。"""
    nums = [v["num"] for v in values if v["num"] is not None]
    if op == "sum":
        return sum(nums), None
    if op == "avg":
        return sum(nums) / len(nums), None
    if op == "count":
        return float(len(values)), None
    pick = max(values, key=lambda v: v["num"]) if op == "max" else min(
        values, key=lambda v: v["num"]
    )
    return pick["num"], pick


def run(question: str, file_id: int | None = None,
        dir_prefix: str | None = None, hint: str = "") -> dict | None:
    """尝试做表格聚合。算不出来返回 None。

    question 是**用户这一轮的原话**，聚合动作（合计/最大/多少个）只从它识别。
    hint 是补充的检索式（多轮追问时带上上文），只用来匹配列名和文件名。

    分开的原因：上一轮问过「最大值是多少」，这一轮问「最小呢」，
    合并后的检索式里「最大」还在 —— 聚合意图会被上一轮劫持，
    用户问的最小值被无视，还会标成「按原始数值精确计算」。
    """
    q = (question or "").strip()
    op = detect_op(q)
    if not op:
        return None
    # 匹配列名/文件名时可以带上上文，但聚合动作只看本轮
    match_text = (hint or "").strip() or q

    rows = _load_rows(file_id, dir_prefix)
    if not rows:
        return None

    num_cols = _numeric_columns(rows)
    if not num_cols:
        return None

    groups = _pick_column_groups(match_text, num_cols)
    if not groups:
        return None

    # 分文件处理：不同文件里的「预算」不是同一笔钱，跨文件相加会把副本算两遍
    named = _named_file(match_text, dir_prefix)

    lines: list[str] = []
    blocks_info: list[dict] = []
    group_blocks: list[tuple[str, list[dict]]] = []
    cited_rows: list[dict] = []
    files_meta: list[dict] = []
    seen_files: set[int] = set()
    used_column = ""
    used_group_col: str | None = None

    for g_stem, g_unit, g_headers in groups:
        items = [i for h in g_headers for i in num_cols[h]]
        values = [i for i in items if i["num"] is not None]
        if op != "count" and len(values) < 2:
            # 只有一行有数字 —— 那不是聚合，是查一个值，交给检索去答
            continue
        if not values:
            continue
        column = f"{g_stem}（{g_unit}）" if g_unit else g_stem
        used_column = used_column or column

        by_file: dict[int, list[dict]] = {}
        for i in values:
            fid = i["row"]["file_id"]
            if named is not None and fid != named:
                continue
            by_file.setdefault(fid, []).append(i)
        if not by_file:
            continue

        group_col = _group_column(match_text, rows, g_stem)
        used_group_col = used_group_col or group_col
        hdr_set = {_clean(h) for h in g_headers}
        blocks: list[tuple[str, list[dict]]] = []
        for fid, fitems in by_file.items():
            blocks.append((_label(fitems[0]["row"]), fitems))
        blocks.sort(key=lambda x: x[0])

        group_lines_all: list[str] = []
        g_blocks: list[dict] = []
        for label, fitems in blocks:
            total, pick = _aggregate(op, fitems)
            if op == "sum":
                body = f"{label}　{column}合计：{fmt_num(total)}"
                detail = f"（{len(fitems)} 项相加）"
            elif op == "count":
                body = f"{label}　{column}共 {int(total)} 项"
                detail = ""
            elif op == "avg":
                body = f"{label}　{column}平均：{fmt_num(total)}"
                detail = f"（{len(fitems)} 项的平均）"
            else:
                body = f"{label}　{column}{'最大' if op == 'max' else '最小'}：{fmt_num(total)}"
                detail = f"（{pick['value']}，{pick['row']['sheet']} 第 {pick['row']['row']} 行）"

            group_lines: list[str] = []
            block_rows: list[dict] = []
            if group_col:
                # 分组列是「类别」这种文本列，要聚合的是**目标列**的数值。
                # 之前错把分组列自己的值拿去求和 —— "运营" 是文本，求和自然为空。
                groups_by_key: dict[str, list[dict]] = {}
                fid = fitems[0]["row"]["file_id"]
                for row in rows:
                    if row["file_id"] != fid:
                        continue
                    key = ""
                    val: dict | None = None
                    for c in row["cells"]:
                        h = _clean(str(c.get("header") or "").strip())
                        if h == _clean(group_col) and not key:
                            key = str(c.get("value") or "").strip()
                        elif h in hdr_set and val is None:
                            val = {
                                "num": to_num(c.get("value")),
                                "value": c.get("value"),
                                "ref": c.get("ref"),
                                "row": row,
                            }
                    if not key or val is None or val["num"] is None:
                        continue
                    if _is_total_label(key):
                        continue
                    groups_by_key.setdefault(key, []).append(val)
                for key in sorted(groups_by_key):
                    gv = groups_by_key[key]
                    gtotal, _ = _aggregate(op, gv)
                    group_lines.append(f"{key}：{fmt_num(gtotal)}")
                    # 每一组的成员都要能核对：只引一行的话，组小计没法验算
                    for item in gv:
                        block_rows.append(item["row"])

            if not block_rows:
                for i in fitems:
                    block_rows.append(i["row"])
            blocks_info.append({"body": body + detail, "group_lines": group_lines,
                                "rows": block_rows})
            g_blocks.append(blocks_info[-1])
            group_lines_all.append(label)
            for r in block_rows:
                if r not in cited_rows:
                    cited_rows.append(r)
            if fitems[0]["row"]["file_id"] not in seen_files:
                seen_files.add(fitems[0]["row"]["file_id"])
                r0 = fitems[0]["row"]
                files_meta.append({"file_id": r0["file_id"], "file_name": r0["file_name"],
                                   "file_path": r0["file_path"], "count": len(fitems)})

        if len(blocks) > 1:
            header = f"「{column}」在 {len(blocks)} 个文件里都有，以下按文件分别列出（不跨文件相加）："
        else:
            header = ""
        group_blocks.append((header, g_blocks))

    if not blocks_info:
        return None
    if len(cited_rows) > MAX_CITATIONS:
        cited_rows = cited_rows[:MAX_CITATIONS]

    # 引用编号：正文里必须带 [n]，否则界面上点不出引用徽章
    idx_of = {id(r): i + 1 for i, r in enumerate(cited_rows)}

    for header, gblocks in group_blocks:
        if header:
            lines.append(header)
        for b in gblocks:
            marks = "".join(f"[{idx_of[id(r)]}]" for r in b["rows"] if id(r) in idx_of)
            lines.append(b["body"] + ((" " + marks) if marks else ""))
            for gl in b["group_lines"]:
                lines.append("　· " + gl)

    hits = [_hit_from_row(r, q) for r in cited_rows]
    return {
        "op": op,
        "column": used_column,
        "group_by": used_group_col,
        "text": "\n".join(lines),
        "hits": hits,
        "files": files_meta,
        "rows_used": len(cited_rows),
    }
