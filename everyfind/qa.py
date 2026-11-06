"""问答层：检索 → 组织上下文 → 生成带引用编号的回答 → 溯源信息。"""
from __future__ import annotations

import re
from typing import Any

from . import compute, db, llm, search as search_mod, textutil
from .config import load_config
from .parsers import SUPPORTED_EXTS

SYSTEM_PROMPT = (
    "你是一个严谨的本地资料库问答助手。"
    "只能依据用户提供的【资料片段】回答问题，严禁编造或使用片段之外的知识。"
    "要求：\n"
    "1. 用中文简洁作答，可用短句分条罗列。\n"
    "2. 每一个结论后面必须标注引用编号，格式如 [1]、[2]，编号对应片段编号，可多个并列如 [1][3]。\n"
    "3. 片段中确实没有答案时，直接回答「资料中未找到相关内容」。\n"
    "4. 保留原文中的数字、日期、金额、责任人等关键信息，不要改写数值。\n"
    "5. 不要输出「资料片段」「参考文档」之类的多余标题。"
)

_CITE_RE = re.compile(r"\[(\d{1,3})\]")

# 文件名识别用「全部支持格式」，别手写一小撮：
# 手写的版本漏掉了 .json/.yaml/.log/.py 这些，
# 导致「config.json 里的配置是什么」这种提问不会被当成文件引用处理。
_EXT_ALT = "|".join(sorted((e.lstrip(".") for e in SUPPORTED_EXTS), key=len, reverse=True))
_FILE_REF_RE = re.compile(r"\S+\.(" + _EXT_ALT + r")\b", re.I)

# 并列提问里「共同关系」的起始标记：验收报告、操作手册分别由谁负责 → 关系 = "由谁负责"
_REL_START = (
    "分别", "各自", "由谁", "谁来", "是谁", "谁负责", "负责人", "负责", "承担",
    "有多少", "多少", "几天", "几周", "几人", "几个", "多大", "多长", "数量",
    "什么", "哪个", "哪些", "哪种", "如何", "怎么", "怎样", "哪里", "在哪",
    "什么时候", "何时", "哪天", "几月", "几日", "第几", "有无", "有没有", "叫什么",
)

# 答案类型 → 句子特征
_ANSWER_TYPES = (
    ("number", re.compile(r"多少|几天|几周|几人|几个|多大|多长|数量|金额|预算|费用|价格")),
    ("person", re.compile(r"谁|何人|哪位|负责人|责任人|由谁")),
    ("time", re.compile(r"什么时候|何时|第几|哪天|几月|几日|日期|时间|周期")),
)

_MULTI_DIGIT = re.compile(r"\d{2,}")
_ANY_DIGIT = re.compile(r"\d")

# 多轮追问：这些开头词说明当前问题是「指代上一轮」的省略句
_FOLLOWUP_MARKERS = (
    "这个", "那个", "它", "他们", "她", "此", "该", "上面", "刚才", "刚刚",
    "之前", "前面", "还有", "同样", "一样", "那么",
)


def _is_followup(question: str) -> bool:
    """判断当前问题是否需要借上一轮的上下文。

    两种信号：实词太少（几乎只剩一个关键词），或者以指代词开头。
    """
    q = (question or "").strip()
    if not q:
        return False
    if len(textutil.content_terms(q)) <= 1:
        return True
    return any(q.startswith(m) for m in _FOLLOWUP_MARKERS)


def _expand_query(question: str, prev_questions: list[str] | None) -> tuple[str, bool]:
    """多轮追问的查询扩展。

    当前问题是省略句时，把上一轮的问题并进检索式，
    让「那预算是多少」也能搜到上一轮话题的内容。

    返回 (检索用的问题, 是否发生了扩展)。
    """
    q = (question or "").strip()
    if not prev_questions or not _is_followup(q):
        return q, False
    prev = [str(p).strip() for p in prev_questions if str(p or "").strip()]
    if not prev:
        return q, False
    return q + " " + prev[-1], True

# 量词/单位：问「几天」就找带「天」的数字
_UNITS = (
    "天", "周", "个月", "月", "年", "日", "元", "块", "人次", "人", "个", "次",
    "项", "条", "件", "台", "份", "米", "倍", "成", "岁", "课时", "页",
)


def _unit_bonus(sentence: str, question: str) -> float:
    """问「放几天」时，含「数字+天」的句子才是答案。"""
    for u in _UNITS:
        if u in (question or "") and re.search(r"\d\s*" + re.escape(u), sentence or ""):
            return 0.28
    return 0.0


def _answer_type(question: str) -> str:
    for name, pat in _ANSWER_TYPES:
        if pat.search(question or ""):
            return name
    return ""


def _type_bonus(sentence: str, atype: str) -> float:
    """按答案类型给句子加分：问数字就该含数字，问人就该含责任人。"""
    if not atype:
        return 0.0
    if atype == "number":
        if _MULTI_DIGIT.search(sentence):
            return 0.30
        if _ANY_DIGIT.search(sentence):
            return 0.10
        return 0.0
    if atype == "person":
        if re.search(r"负责|由|经理|主管|负责人|专员|工程师|总监|组长|owner", sentence):
            return 0.26
        return 0.0
    if atype == "time":
        if re.search(r"第\s*\d|\d{4}\s*年|\d{1,2}\s*月|\d{1,2}\s*日|\d+\s*周|第[一二三四五六七八九十]+", sentence):
            return 0.26
        return 0.0
    return 0.0


def _find_relation_start(text: str) -> int:
    """找出并列项尾部里「关系短语」的起始位置；找不到返回 -1。"""
    for i in range(len(text)):
        for mark in _REL_START:
            if text.startswith(mark, i):
                return i
    return -1


def _plan_queries(question: str) -> tuple[list[str], str]:
    """把提问拆成若干条检索式。

    返回 (检索式列表, 模式)：
    - parallel：并列项 + 共同关系（「A、B、C分别由谁负责」→ 逐项作答）
    - multi：多个并列问句（「预算是多少？实际支出多少？」→ 合并成一次检索）
    - single：普通单问句
    """
    q = (question or "").strip()

    items = [p.strip() for p in q.split("、") if p.strip()]
    if len(items) >= 2:
        tail = items[-1]
        pos = _find_relation_start(tail)
        if pos > 0:
            relation = tail[pos:]
            topics = items[:-1] + [tail[:pos]]
            probes = [
                f"{t.strip()} {relation}".strip()
                for t in topics
                if t.strip(" ，,。?？!！")
            ]
            if len(probes) >= 2:
                return probes, "parallel"

    parts = [p.strip() for p in re.split(r"[？?！!。；;]", q) if p.strip()]
    parts = [p for p in parts if not _FILE_REF_RE.search(p)]
    parts = [re.sub(r"^(分别|各自|各|那么)", "", p).strip() for p in parts]
    parts = [p for p in parts if len(p) >= 2]
    if len(parts) >= 2:
        return parts, "multi"
    return [q], "single"


def _fmt_loc(hit: dict) -> str:
    return hit.get("loc_label") or "正文"


def build_context(hits: list[dict]) -> str:
    parts: list[str] = []
    for i, h in enumerate(hits, 1):
        parts.append(
            f"[{i}] 文件：{h['file_name']} · 位置：{_fmt_loc(h)}\n"
            f"{h['text'].strip()}"
        )
    return "\n\n".join(parts)


def _pick_cell(hit: dict, query: str) -> dict | None:
    """在 Excel 行块里挑出与提问最相关的单元格，用于精确到 B4 级别的引用。

    表头（字段名）权重高于单元格值：问「预算是多少」，答案在「预算（元）」那一格。
    """
    if hit.get("loc_type") != "cell":
        return None
    try:
        import json

        loc = json.loads(hit.get("loc_json") or "{}")
    except Exception:
        return None
    cells = loc.get("cells") or []
    if not cells:
        return None

    grams = set(textutil.char_bigrams(query))
    terms = set(textutil.content_terms(query))
    best, best_score = None, -1.0
    for c in cells:
        value = str(c.get("value") or "")
        header = str(c.get("header") or "")
        if not value:
            continue
        hg = set(textutil.char_bigrams(header))
        vg = set(textutil.char_bigrams(value))
        ht = set(textutil.tokenize(header))
        vt = set(textutil.tokenize(value))
        score = 0.0
        if grams:
            score += 2.0 * len(grams & hg) / len(grams)
            score += 1.0 * len(grams & vg) / len(grams)
        if terms:
            score += 1.5 * len(terms & ht) / len(terms)
            score += 0.8 * len(terms & vt) / len(terms)
        # 问题里直接写出了这个数值，则该格就是答案（只对数字型单元格生效，
        # 否则「差旅费」这种行标签会因为出现在问题里而压过真正的数值列）
        q_norm = (query or "").replace(",", "")
        if re.search(r"\d", value) and value.replace(",", "") in q_norm:
            score += 1.0
        if score > best_score:
            best_score, best = score, c
    if best is None or best_score <= 0.35:
        return None
    return best


def _citation(hit: dict, n: int, query: str) -> dict:
    c: dict[str, Any] = {
        "n": n,
        "chunk_id": hit["chunk_id"],
        "file_id": hit["file_id"],
        "file_name": hit["file_name"],
        "file_path": hit["file_path"],
        "file_ext": hit["file_ext"],
        "rel_dir": hit.get("file_rel_dir") or "",
        "loc_type": hit["loc_type"],
        "loc_label": hit["loc_label"],
        "loc_json": hit.get("loc_json"),
        "text": hit["text"],
        "snippet": hit.get("snippet") or textutil.snippet(hit["text"], query),
        "spans": hit.get("spans") or [],
    }
    cell = _pick_cell(hit, query)
    if cell:
        c["focus_cell"] = cell
        c["loc_label"] = f"{hit['loc_label']} · 单元格 {cell.get('ref')}"
    return c


def _best_sentence_for(hits: list[dict], probe: str,
                        max_items: int = 2) -> list[tuple[int, str, float]]:
    """在给定片段里找与 probe 最相关的句子。

    打分 = 二元组覆盖率 + 0.35 × 新颖度 + 答案类型加分。
    同分时取更短的句子（短句往往更直接）。
    返回 [(片段下标(1起), 句子, 分数)]。
    """
    atype = _answer_type(probe)
    out: list[tuple[int, str, float]] = []
    for i, h in enumerate(hits, 1):
        text = h.get("text") or ""
        if not text:
            continue
        best_score = -1.0
        best_sent = ""
        for sent, _s, _e, cov in textutil.sentence_candidates(text, probe):
            if cov <= 0:
                continue
            score = cov + 0.35 * textutil.novelty_ratio(sent, probe) + _type_bonus(sent, atype) + _unit_bonus(sent, probe)
            if (score, -len(sent)) > (best_score, -len(best_sent) if best_sent else 0):
                best_score, best_sent = score, sent
        if best_sent:
            out.append((i, best_sent, best_score))
    out.sort(key=lambda x: (-x[2], len(x[1])))
    if out:
        # 相对门槛：明显弱于最优句的片段不进答案，避免混进目录/表头之类的噪声
        floor = out[0][2] * 0.6
        out = [x for x in out if x[2] >= floor]
    return out[:max_items]


def _digits(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", (s or "").replace(",", "")))


def _extractive_answer(hits: list[dict], query: str,
                      probes: list[str] | None = None,
                      mode: str = "single",
                      probe_hits: dict[str, list[dict]] | None = None) -> tuple[str, list[int]]:
    """无模型时的降级：直接给出原文中最相关的句子。

    parallel：逐项作答；multi：合并检索后给最贴切的一段；single：给最相关的若干段。
    """
    if probes is None:
        probes, mode = _plan_queries(query)
    probe_hits = probe_hits or {}
    lines: list[str] = []
    used: list[int] = []
    seen: set[str] = set()
    seen_digits: list[set[str]] = []
    index_of = {h["chunk_id"]: n for n, h in enumerate(hits, 1)}

    def add(idx: int, sent: str) -> None:
        key = re.sub(r"\s+", "", sent)[:40]
        if key in seen:
            return
        dg = _digits(sent)
        # 只在「数字集合完全一致」时判为重复事实。
        # 早先用集合子集判定（dg <= prev），会把「第 4 周开始验收」
        # 这类被前一句「共 8 天，第 4 周」包含的不同事实误杀。
        if dg and any(dg == prev for prev in seen_digits):
            return  # 数字完全一致，说明是同一事实的重复表述
        seen.add(key)
        if dg:
            seen_digits.append(dg)
        sent = sent.strip()
        if len(sent) > 240:
            sent = sent[:240] + "…"
        lines.append(f"{sent} [{idx}]")
        used.append(idx)

    if mode == "parallel":
        for probe in probes:
            pool = probe_hits.get(probe) or hits
            picked = _best_sentence_for(pool, probe, max_items=1)
            if not picked:
                continue
            pos, sent, _score = picked[0]
            main_idx = index_of.get(pool[pos - 1]["chunk_id"])
            if main_idx is None:
                continue
            add(main_idx, sent)
    elif mode == "multi":
        combined = " ".join(probes)
        grams = set(textutil.char_bigrams(combined))
        atype = _answer_type(combined)
        scored: list[tuple[float, float, int, dict]] = []
        for n, h in enumerate(hits, 1):
            cg = set(textutil.char_bigrams(h["text"] or ""))
            cov = (len(grams & cg) / len(grams)) if grams else 0.0
            scored.append((cov, float(h.get("score", 0.0)), n, h))
        scored.sort(key=lambda x: (-round(x[0], 4), -x[1]))
        if scored:
            best_cov = scored[0][0]
            keep = [s for s in scored if s[0] >= best_cov * 0.85][:2]
            for _cov, _cs, n, h in keep:
                best = (0.0, "")
                for sent, _s, _e, cov in textutil.sentence_candidates(h["text"] or "", combined):
                    score = cov + 0.35 * textutil.novelty_ratio(sent, combined) + _type_bonus(sent, atype) + _unit_bonus(sent, combined)
                    if (score, -len(sent)) > (best[0], -len(best[1]) if best[1] else 0):
                        best = (score, sent)
                if best[1]:
                    add(n, best[1])
    else:
        probe = probes[0] if probes else query
        for idx, sent, _score in _best_sentence_for(hits, probe, max_items=4):
            add(idx, sent)

    if not lines:
        for idx, sent, _cov in _best_sentence_for(hits, query, max_items=2):
            add(idx, sent)

    return "\n\n".join(lines), used


def answer(question: str, cfg: dict | None = None, top_k: int | None = None,
           prev_questions: list[str] | None = None,
           file_id: int | None = None,
           dir_prefix: str | None = None) -> dict:
    cfg = cfg or load_config()
    rcfg = cfg.get("retrieve") or {}
    top_k = int(top_k or rcfg.get("top_k", 8))
    candidates = int(rcfg.get("candidates", 60))
    # 送进模型的片段数上限。top_k 是软目标，但并列提问每个子问题
    # 至少要各留一个片段，所以下限是子问题数；两者都再受 max_context_chunks 封顶。
    max_ctx = max(int(rcfg.get("max_context_chunks", 16)), 1)

    question = (question or "").strip()
    if not question:
        return {
            "answer": "请输入问题。",
            "citations": [],
            "cited": [],
            "files": [],
            "mode": "none",
            "degraded": False,
            "error": "",
            "expanded": False,
        }

    # 多轮追问：省略句借上一轮的上下文做检索扩展
    query, expanded = _expand_query(question, prev_questions)
    tail = query[len(question):].strip() if expanded else ""

    hits = search_mod.retrieve(query, top_k=top_k, candidates=candidates,
                               file_id=file_id, dir_prefix=dir_prefix)

    # 表格聚合计算：意图明确（合计/共多少个/最大…）且能唯一定位到数值列时，
    # 直接用算出来的结果作答 —— 数字是按表里原始值算的，比让模型转述可靠，
    # 也省一次云端调用。算不出来就照常走检索流程。
    # 聚合动作只看**本轮原话**（question），扩展出来的检索式只用来匹配列名 ——
    # 否则上一轮问过「最大值」，这一轮问「最小呢」会被上一轮的意图带跑。
    computed = compute.run(question, file_id=file_id, dir_prefix=dir_prefix, hint=query)

    # 并列式提问：按子项再检索一轮，把相关片段补进上下文
    segments, plan_mode = _plan_queries(question)
    seg_hits: dict[str, list[dict]] = {}
    probes: list[str] = list(segments)
    if computed and computed["hits"]:
        # 已经有算好的结果，不再做并列拆解
        hits = computed["hits"]
        segments, probes, plan_mode = [question], [question], "single"
    elif len(segments) >= 2:
        # 子问题数也受上限约束，否则「十几个子问题」会把上下文撑爆
        segments = segments[:max_ctx]
        probes = [(s + " " + tail).strip() if tail else s for s in segments]

        # 上下文预算：至少容纳每个子问题各一个片段
        budget = min(max_ctx, max(top_k, len(segments)))
        known: set[int] = set()
        merged: list[dict] = []

        def _push(h: dict) -> None:
            cid = int(h["chunk_id"])
            if cid not in known:
                known.add(cid)
                merged.append(h)

        # 先放每个子问题的最佳片段，保证并列提问每一项都有代表
        for probe in probes:
            sh = search_mod.search(probe, limit=5, file_id=file_id, dir_prefix=dir_prefix)
            seg_hits[probe] = sh
            if sh:
                _push(sh[0])
        # 再用整体检索命中的其余片段补足
        for h in hits:
            if len(merged) >= budget:
                break
            _push(h)
        hits = merged[:budget]

    if not hits:
        return {
            "answer": "资料中未找到相关内容。可以换个说法，或先把相关文件加入资料库。",
            "citations": [],
            "cited": [],
            "files": [],
            "mode": "empty",
            "degraded": False,
            "error": "",
            "expanded": expanded,
        }

    citations = [_citation(h, i, question) for i, h in enumerate(hits, 1)]
    context = build_context(hits)

    mode = "extractive"
    error = ""
    text = ""
    if computed and computed["hits"]:
        # 聚合计算：直接给算式与结果，不经模型转述
        mode = "computed"
        text = computed["text"]
        cited = list(range(1, len(citations) + 1))
    else:
        if llm.is_configured(cfg):
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"【资料片段】\n{context}\n\n【问题】\n{question}",
                },
            ]
            text, error = llm.chat(messages, cfg)
            if text:
                mode = "llm"
            else:
                # 调了模型却什么都没拿到 —— 必须算「失败」，
                # 否则会落成「未接入模型」，把调用过说成没配置
                if not error:
                    error = "模型返回了空内容"
                text = ""

        if mode != "llm":
            text, used = _extractive_answer(hits, query, probes, plan_mode, seg_hits)
            if not text:
                text = "资料中未找到可直接回答的内容，请换个问法或补充相关文件。"
            cited = used
        else:
            cited = sorted({int(m) for m in _CITE_RE.findall(text) if 1 <= int(m) <= len(citations)})
            if not cited:
                cited = list(range(1, min(len(citations), 3) + 1))

    files = []
    seen: set[int] = set()
    for c in citations:
        if c["n"] not in cited:
            continue
        if c["file_id"] in seen:
            continue
        seen.add(c["file_id"])
        files.append(
            {
                "file_id": c["file_id"],
                "file_name": c["file_name"],
                "file_path": c["file_path"],
                "count": sum(1 for x in citations if x["file_id"] == c["file_id"] and x["n"] in cited),
            }
        )

    # 降级措辞要区分三种情况：
    # - 压根没配模型 → 「未接入大模型」
    # - 配了但调用失败 → 「模型调用失败，已降级」，不能谎称没接
    # - 走了聚合计算 → 「表格聚合计算」，这是精确值，不是摘录也不是模型生成
    degraded = bool(error)
    if mode == "computed":
        text = f"{text}\n\n（表格聚合计算结果：按表中原始数值计算，未使用模型。）"
    elif mode != "llm":
        if degraded:
            text = f"{text}\n\n（模型调用失败，已降级为资料原文摘录。{error}）"
        else:
            text = f"根据资料检索，原文摘录如下：\n\n{text}\n\n（未接入大模型，以上为资料原文摘录。）"

    return {
        "answer": text,
        "citations": citations,
        "cited": cited,
        "files": files,
        "mode": mode,
        "degraded": degraded,
        "error": error,
        "expanded": expanded,
    }


def resolve_citations(question: str, cfg: dict | None = None,
                      file_id: int | None = None) -> list[dict]:
    """只做检索、不调用模型，把问题解析成带编号的引用列表。

    给「点开一条旧回答里的引用」这类兜底场景用：
    不重新调模型，避免白白烧掉一次云端调用。
    """
    cfg = cfg or load_config()
    rcfg = cfg.get("retrieve") or {}
    top_k = int(rcfg.get("top_k", 8))
    candidates = int(rcfg.get("candidates", 60))
    question = (question or "").strip()
    if not question:
        return []
    hits = search_mod.retrieve(question, top_k=top_k, candidates=candidates, file_id=file_id)
    return [_citation(h, i, question) for i, h in enumerate(hits, 1)]


def source_detail(chunk_id: int, question: str = "") -> dict | None:
    """取某个引用块的完整原文 + 高亮定位。"""
    row = db.get_chunk(chunk_id)
    if not row:
        return None
    text = row["text"] or ""
    detail: dict[str, Any] = {
        "chunk_id": chunk_id,
        "file_id": int(row["file_id"]),
        "file_name": row["file_name"],
        "file_path": row["file_path"],
        "file_ext": row["file_ext"],
        "loc_type": row["loc_type"],
        "loc_label": row["loc_label"],
        "loc_json": row["loc_json"],
        "text": text,
        "spans": [],
        "sentence": "",
    }
    if question:
        detail["spans"] = textutil.highlight_spans(text, question)
        sent, s, e = textutil.best_sentence(text, question)
        detail["sentence"] = sent
        detail["sentence_span"] = [s, e]
    if detail["loc_type"] == "cell":
        cell = _pick_cell(
            {
                "loc_type": "cell",
                "loc_json": row["loc_json"],
                "text": text,
            },
            question or text,
        )
        if cell:
            detail["focus_cell"] = cell
            detail["loc_label"] = f"{row['loc_label']} · 单元格 {cell.get('ref')}"
    return detail
