"""中文友好的文本处理：分词、字符二元组、停用词、高亮定位。"""
from __future__ import annotations

import re
from typing import Iterable

try:
    import jieba

    jieba.setLogLevel(60)
    _HAS_JIEBA = True
except Exception:  # pragma: no cover
    jieba = None
    _HAS_JIEBA = False

CJK = r"一-鿿㐀-䶿"
_CJK_RE = re.compile(f"[{CJK}]")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_PUNCT_RE = re.compile(r"[\s　!-/:-@\[-`{-~，。、；：？！“”‘’（）《》【】…—·]+")

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "有", "为", "以", "等", "把", "被",
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "这", "那", "这个", "那个",
    "一个", "什么", "怎么", "如何", "哪些", "哪个", "多少", "请问", "吗", "呢", "吧",
    "啊", "呀", "哦", "嗯", "一下", "些", "都", "就", "还", "也", "很", "并", "而",
    "但", "所以", "因为", "如果", "可以", "需要", "进行", "通过", "对于", "关于", "以及",
    "a", "an", "the", "is", "are", "was", "were", "be", "of", "to", "in", "on", "for",
    "and", "or", "what", "how", "which", "who", "when", "please", "do", "does", "did",
}


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def tokenize(text: str) -> list[str]:
    """分词：中文用 jieba（回退到字符二元组），英文数字按词。"""
    if not text:
        return []
    out: list[str] = []
    if _HAS_JIEBA:
        for w in jieba.cut(text.lower()):
            w = w.strip()
            if not w or _PUNCT_RE.fullmatch(w):
                continue
            if w in STOPWORDS:
                continue
            out.append(w)
    else:
        out = [c for c in text.lower() if c.strip() and not _PUNCT_RE.match(c)]

    # 英文/数字词始终保留（jieba 会切出奇怪的碎片）
    for w in _ASCII_WORD_RE.findall(text.lower()):
        if w not in STOPWORDS and len(w) >= 2:
            out.append(w)
    return out


def content_terms(text: str) -> list[str]:
    """用于检索的"实词"：去掉停用词与单字虚词。"""
    terms: list[str] = []
    for t in tokenize(text):
        if t in STOPWORDS:
            continue
        if len(t) == 1 and not t.isdigit():
            continue
        terms.append(t)
    return terms


def char_bigrams(text: str) -> list[str]:
    """字符二元组，中文子串召回的主力。"""
    text = _PUNCT_RE.sub(" ", text or "").lower()
    grams: list[str] = []
    for token in text.split():
        if len(token) == 1:
            if _CJK_RE.match(token) or token.isdigit():
                grams.append(token)
            continue
        if _CJK_RE.search(token):
            for i in range(len(token) - 1):
                grams.append(token[i : i + 2])
        for w in _ASCII_WORD_RE.findall(token):
            if len(w) >= 2:
                grams.append(w)
    return grams


def build_fts_query(terms: Iterable[str]) -> str:
    """构造 FTS5 MATCH 查询：每个词组加引号后 OR 连接。"""
    quoted = []
    seen = set()
    for t in terms:
        t = t.replace('"', "")
        if not t or t in seen:
            continue
        seen.add(t)
        quoted.append(f'"{t}"')
    return " OR ".join(quoted)


def highlight_spans(text: str, query: str, max_spans: int = 40) -> list[list[int]]:
    """在原文中定位与提问最相关的片段，返回 [start, end) 区间列表。

    策略：优先匹配问句里的长词/连续片段，再用二元组覆盖补充。
    """
    if not text or not query:
        return []

    spans: list[tuple[int, int]] = []

    # 1) 直接子串匹配：问句中较长的片段
    q_clean = _PUNCT_RE.sub("", query)
    for n in range(min(len(q_clean), 20), 1, -1):
        for i in range(0, len(q_clean) - n + 1):
            frag = q_clean[i : i + n]
            if len(frag) < 2:
                continue
            start = 0
            while True:
                pos = text.find(frag, start)
                if pos < 0:
                    break
                spans.append((pos, pos + len(frag)))
                start = pos + len(frag)
                if len(spans) > max_spans:
                    break
            if len(spans) > max_spans:
                break
        if len(spans) >= 3:
            break

    # 2) 二元组匹配补充
    if len(spans) < max_spans:
        grams = [g for g in char_bigrams(query) if len(g) >= 2]
        for g in grams[:60]:
            start = 0
            while True:
                pos = text.find(g, start)
                if pos < 0:
                    break
                spans.append((pos, pos + len(g)))
                start = pos + len(g)
                if len(spans) > max_spans:
                    break
            if len(spans) > max_spans:
                break

    # 合并重叠区间
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged[:max_spans]


def _logical_lines(text: str) -> list[str]:
    """把可能折行的文本还原成逻辑行。

    扫描件/OCR 出来的文字常常在句子中间换行（上一行以「、，,；;：:」结尾），
    这里把这种行合并回来，避免答案被切碎。
    """
    raw = [ln for ln in (text or "").split("\n")]
    out: list[str] = []
    for line in raw:
        stripped = line.strip()
        if not stripped:
            continue
        # 只有明显是「没说完」的标点才合并。
        # 原来把「）」「)」也算进去，会把「（已完成）」这种完整句子
        # 和下一行硬拼在一起，制造跨句的假句子。
        if out and out[-1].rstrip().endswith(("、", "，", ",", "；", ";", "：", ":")):
            out[-1] = out[-1].rstrip() + stripped
        else:
            out.append(stripped)
    return out


def novelty_ratio(sentence: str, query: str) -> float:
    """句子中「问题里没有」的信息占比（上限 0.6）。

    高新颖度 = 这句话带来了新信息（答案），而不是复述提问里的词。
    上限是为了避免"长句子天然新颖"把短而直接的答案挤掉。
    """
    sg = set(char_bigrams(sentence))
    if not sg:
        return 0.0
    qg = set(char_bigrams(query))
    return min(0.6, len(sg - qg) / len(sg))


def sentence_candidates(text: str, query: str) -> list[tuple[str, int, int, float]]:
    """把文本切成句子，逐句计算与提问的相关度。

    返回 [(句子, start, end, coverage)]，coverage = 查询字符二元组被该句覆盖的比例。
    """
    if not text:
        return []

    grams = set(char_bigrams(query))
    terms = set(content_terms(query))

    out: list[tuple[str, int, int, float]] = []
    for logical in _logical_lines(text):
        pieces = [p for p in re.split(r"(?<=[。！？；!?;])", logical) if p.strip()]
        if not pieces:
            pieces = [logical]
        offset = 0
        cursor = 0
        for piece in pieces:
            core = piece.strip()
            if not core:
                offset += len(piece)
                continue
            pg = set(char_bigrams(core))
            g_hit = len(grams & pg)
            coverage = (g_hit / len(grams)) if grams else 0.0
            t_hit = len(terms & set(tokenize(core)))
            score = coverage + 0.25 * (t_hit / len(terms) if terms else 0.0)
            if len(core) > 300:
                score *= 0.7
            # 从上次的位置往后找：同一句话在一个块里出现两次时，
            # 两次都用 text.find(core) 会让高亮都落在第一处。
            pos = text.find(core, cursor)
            if pos < 0:
                pos = text.find(core)
            if pos < 0:
                pos = offset
            cursor = pos + len(core)
            out.append((core, pos, pos + len(core), score))
            offset += len(piece)
    return out


def best_sentence(text: str, query: str) -> tuple[str, int, int]:
    """找出与提问最相关的一句话，返回 (句子, 在原文中的 start, end)。"""
    cands = sentence_candidates(text, query)
    if not cands:
        return "", 0, 0
    best = max(cands, key=lambda c: c[3])
    return best[0], best[1], best[2]


def snippet(text: str, query: str, width: int = 220) -> str:
    """给搜索结果生成一段带上下文的摘要。"""
    if not text:
        return ""
    spans = highlight_spans(text, query)
    if not spans:
        return text[:width].replace("\n", " ") + ("…" if len(text) > width else "")
    s, e = spans[0][0], spans[0][1]
    start = max(0, s - width // 3)
    end = min(len(text), max(e + width // 3, start + width))
    out = text[start:end].replace("\n", " ").strip()
    if start > 0:
        out = "…" + out
    if end < len(text):
        out = out + "…"
    return out
