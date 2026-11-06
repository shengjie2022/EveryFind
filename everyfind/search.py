"""混合检索：中文分词 FTS5 + 字符二元组 FTS5 + 覆盖率重排。"""
from __future__ import annotations

import re
from typing import Any

from . import db, textutil

_PUNCT = re.compile(r"[\s　!-/:-@\[-`{-~，。、；：？！“”‘’（）《》【】…—·]+")


def _clean(q: str) -> str:
    return _PUNCT.sub("", q or "")


def _file_name_hints(query: str) -> list[str]:
    """问句里提到的文件名（用于加权）。"""
    hints: list[str] = []
    conn = db.connect()
    q = query.lower()
    for r in conn.execute("SELECT name FROM files WHERE name IS NOT NULL"):
        name = (r["name"] or "").lower()
        stem = name.rsplit(".", 1)[0]
        if len(stem) >= 2 and stem in q:
            hints.append(name)
    return hints


def _fts_rows(conn, table: str, match: str, limit: int, file_id: int | None,
              dir_prefix: str | None = None):
    """在 FTS5 表上做 MATCH 查询，可选限定到某个文件或某个目录。

    file_id / dir_prefix 必须下推到 SQL 里：先取全局前 N 个候选、再在 Python 里
    按文件过滤的话，目标文件不在候选池里时就会返回空。
    """
    dir_clause, dir_args = db.dir_scope(dir_prefix or "")

    if file_id is None:
        if not dir_clause:
            return conn.execute(
                f"SELECT rowid, bm25({table}) s FROM {table} "
                f"WHERE {table} MATCH ? ORDER BY s LIMIT ?",
                (match, limit),
            ).fetchall()
        return conn.execute(
            f"SELECT t.rowid AS rowid, bm25({table}) s FROM {table} t "
            f"JOIN chunks c ON c.id = t.rowid JOIN files f ON f.id = c.file_id "
            f"WHERE {table} MATCH ?{dir_clause} ORDER BY s LIMIT ?",
            (match, *dir_args, limit),
        ).fetchall()
    if not dir_clause:
        return conn.execute(
            f"SELECT t.rowid AS rowid, bm25({table}) s FROM {table} t "
            f"JOIN chunks c ON c.id = t.rowid "
            f"WHERE {table} MATCH ? AND c.file_id = ? ORDER BY s LIMIT ?",
            (match, file_id, limit),
        ).fetchall()
    return conn.execute(
        f"SELECT t.rowid AS rowid, bm25({table}) s FROM {table} t "
        f"JOIN chunks c ON c.id = t.rowid JOIN files f ON f.id = c.file_id "
        f"WHERE {table} MATCH ? AND c.file_id = ?{dir_clause} ORDER BY s LIMIT ?",
        (match, file_id, *dir_args, limit),
    ).fetchall()


def _fts_candidates(terms: list[str], grams: list[str], limit: int,
                    file_id: int | None = None,
                    dir_prefix: str | None = None) -> dict[int, dict]:
    """返回 {chunk_id: {"tok": s, "gram": s}}。"""
    conn = db.connect()
    out: dict[int, dict] = {}

    if terms:
        q = textutil.build_fts_query(terms)
        if q:
            try:
                rows = _fts_rows(conn, "chunks_tok", q, limit, file_id, dir_prefix)
                for r in rows:
                    out.setdefault(int(r["rowid"]), {})["tok"] = -float(r["s"])
            except Exception:
                pass

    if grams:
        q = textutil.build_fts_query(grams)
        if q:
            try:
                rows = _fts_rows(conn, "chunks_gram", q, limit, file_id, dir_prefix)
                for r in rows:
                    out.setdefault(int(r["rowid"]), {})["gram"] = -float(r["s"])
            except Exception:
                pass
    return out


def _like_candidates(terms: list[str], limit: int,
                     file_id: int | None = None,
                     dir_prefix: str | None = None) -> dict[int, dict]:
    """FTS 无结果时的兜底：LIKE 粗筛。"""
    conn = db.connect()
    out: dict[int, dict] = {}
    dir_clause, dir_args = db.dir_scope(dir_prefix or "")
    for t in terms[:6]:
        if len(t) < 2:
            continue
        try:
            # 词里可能含 % 或 _，不转义会被当成通配符，导致误召回
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql = ("SELECT c.id FROM chunks c JOIN files f ON f.id=c.file_id "
                   "WHERE c.text LIKE ? ESCAPE '\\'")
            args: list[Any] = [f"%{esc}%"]
            if file_id is not None:
                sql += " AND c.file_id = ?"
                args.append(file_id)
            if dir_clause:
                sql += dir_clause
                args.extend(dir_args)
            sql += " LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
        except Exception:
            continue
        for r in rows:
            out.setdefault(int(r["id"]), {})["like"] = 1.0
        if out:
            break
    return out


def search(query: str, limit: int = 20, file_id: int | None = None,
           candidates: int = 60, dir_prefix: str | None = None) -> list[dict]:
    """关键词检索。返回带分数、摘要、高亮的命中列表。"""
    query = (query or "").strip()
    if not query:
        return []

    terms = textutil.content_terms(query)
    grams = [g for g in textutil.char_bigrams(query) if len(g) >= 2]
    q_grams = set(grams)
    q_clean = _clean(query)

    pool = _fts_candidates(terms, grams, candidates, file_id=file_id, dir_prefix=dir_prefix)
    if not pool:
        pool = _like_candidates(terms, candidates, file_id=file_id, dir_prefix=dir_prefix)
    if not pool:
        return []

    ids = list(pool.keys())
    conn = db.connect()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT c.id, c.file_id, c.ord, c.loc_type, c.loc_label, c.loc_json, c.text,
                   f.name AS file_name, f.path AS file_path, f.ext AS file_ext,
                   f.rel_dir AS file_rel_dir
            FROM chunks c JOIN files f ON f.id=c.file_id
            WHERE c.id IN ({placeholders})""",
        ids,
    ).fetchall()

    max_tok = max((v.get("tok", 0) for v in pool.values()), default=0.0) or 1.0
    max_gram = max((v.get("gram", 0) for v in pool.values()), default=0.0) or 1.0
    hints = _file_name_hints(query)

    hits: list[dict] = []
    for r in rows:
        cid = int(r["id"])
        if file_id is not None and int(r["file_id"]) != file_id:
            continue
        text = r["text"] or ""
        sc = pool.get(cid, {})

        tok = float(sc.get("tok", 0.0)) / max_tok
        gram = float(sc.get("gram", 0.0)) / max_gram
        c_grams = set(textutil.char_bigrams(text))
        coverage = (len(q_grams & c_grams) / len(q_grams)) if q_grams else 0.0
        exact = 1.0 if (len(q_clean) >= 3 and q_clean in text) else 0.0
        term_hit = (
            len(set(terms) & set(textutil.tokenize(text))) / len(set(terms))
            if terms else 0.0
        )

        score = 0.30 * tok + 0.26 * gram + 0.24 * coverage + 0.12 * term_hit + 0.08 * exact
        if sc.get("like"):
            score += 0.05
        if any(h in query.lower() for h in hints):
            score += 0.12
        # 短内容块通常更精准
        if len(text) < 40:
            score += 0.02

        hits.append(
            {
                "chunk_id": cid,
                "file_id": int(r["file_id"]),
                "file_name": r["file_name"],
                "file_path": r["file_path"],
                "file_ext": r["file_ext"],
                "file_rel_dir": r["file_rel_dir"] or "",
                "ord": int(r["ord"]),
                "loc_type": r["loc_type"],
                "loc_label": r["loc_label"],
                "loc_json": r["loc_json"],
                "text": text,
                "score": round(score, 4),
                "snippet": textutil.snippet(text, query),
                "spans": textutil.highlight_spans(text, query),
            }
        )

    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def retrieve(query: str, top_k: int = 8, candidates: int = 60,
             file_id: int | None = None, dir_prefix: str | None = None) -> list[dict]:
    """为问答准备上下文：去重 + 同文件邻近块合并 + 多样性控制。"""
    hits = search(query, limit=max(top_k * 4, 20), candidates=candidates,
                  file_id=file_id, dir_prefix=dir_prefix)
    if not hits:
        return []

    # 相对分数门槛：明显不相关的片段不进上下文
    top_score = hits[0]["score"] or 1.0
    floor = top_score * 0.30
    kept = [h for h in hits if h["score"] >= floor]
    if len(kept) < 3:
        kept = hits[:3]
    hits = kept

    picked: list[dict] = []
    per_file: dict[int, int] = {}

    for h in hits:
        fid = h["file_id"]
        if per_file.get(fid, 0) >= max(3, top_k // 2):
            continue
        if any(abs(p["ord"] - h["ord"]) <= 1 and p["file_id"] == fid for p in picked):
            continue
        picked.append(h)
        per_file[fid] = per_file.get(fid, 0) + 1
        if len(picked) >= top_k:
            break

    if len(picked) < top_k:
        for h in hits:
            if h in picked:
                continue
            picked.append(h)
            if len(picked) >= top_k:
                break
    return picked
