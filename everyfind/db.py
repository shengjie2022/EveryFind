"""SQLite 索引库：文件表、内容块表、FTS5 全文索引。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    ext         TEXT,
    size        INTEGER DEFAULT 0,
    mtime       REAL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending|indexing|ok|error|skipped
    error       TEXT,
    chunk_count INTEGER DEFAULT 0,
    meta        TEXT,
    source      TEXT DEFAULT 'file',               -- folder|file|disk|upload
    rel_dir     TEXT DEFAULT '',                   -- 相对资料根目录的目录（2026/预算）
    added_at    REAL,
    indexed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_name   ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_reldir ON files(rel_dir);

CREATE TABLE IF NOT EXISTS qa_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    mode        TEXT,
    degraded    INTEGER DEFAULT 0,
    expanded    INTEGER DEFAULT 0,
    file_id     INTEGER,
    file_name   TEXT,
    citations   TEXT,
    cited       TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_ts ON qa_history(ts DESC);

CREATE TABLE IF NOT EXISTS roots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL DEFAULT 'folder',     -- folder|disk
    added_at  REAL,
    last_scan REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord       INTEGER NOT NULL,
    loc_type  TEXT NOT NULL,      -- paragraph|cell|slide|page|line|image|table|header
    loc_label TEXT NOT NULL,      -- 人类可读定位：段落 6 / 第3页 / Sheet1·B4 / 第4张幻灯片
    loc_json  TEXT,               -- 结构化定位
    text      TEXT NOT NULL,
    nchar     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_ord  ON chunks(file_id, ord);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tok  USING fts5(tokens, tokenize="unicode61 remove_diacritics 0");
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_gram USING fts5(grams,  tokenize="unicode61 remove_diacritics 0");
"""

_local = threading.local()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """每个线程一个连接。"""
    # 键要用路径本身，不能用 id(path)：Path 对象回收后 id 会被复用，
    # 新建的另一个路径可能拿到同一个 id，于是拿到**别的库**的连接。
    key = f"conn_{path}" if path else "conn_default"
    conn = getattr(_local, key, None)
    if conn is None:
        p = path or config.db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        setattr(_local, key, conn)
    return conn


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError:
        # 老库缺新列时，SCHEMA 里的 CREATE INDEX 会先失败（列还不存在），
        # 于是后面补列的 _migrate 根本没机会跑。先补列，再重跑一次建表脚本。
        _migrate(conn)
        conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补上后加的列。

    CREATE TABLE IF NOT EXISTS 对已存在的表不会改结构，
    所以新列必须显式 ALTER，否则老用户的 index.db 会一直缺列。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
    ).fetchone()
    if not exists:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
    if "rel_dir" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN rel_dir TEXT DEFAULT ''")


def reset_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError:
        _migrate(conn)
        conn.executescript(SCHEMA)
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM roots")
    conn.execute("DELETE FROM chunks_tok")
    conn.execute("DELETE FROM chunks_gram")
    conn.commit()


# ---------------------------------------------------------------- 文件

def upsert_file(path: str, name: str, ext: str, size: int, mtime: float,
                source: str = "file", rel_dir: str = "") -> int:
    conn = connect()
    now = _now()
    conn.execute(
        """INSERT INTO files(path,name,ext,size,mtime,status,source,rel_dir,added_at)
           VALUES(?,?,?,?,?,'pending',?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             name=excluded.name, ext=excluded.ext, size=excluded.size,
             mtime=excluded.mtime, status='pending', error=NULL,
             source=excluded.source,
             rel_dir=CASE WHEN excluded.rel_dir <> '' THEN excluded.rel_dir
                          ELSE files.rel_dir END""",
        (path, name, ext, size, mtime, source, rel_dir, now),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
    return int(row["id"])


def set_rel_dir(file_id: int, rel_dir: str) -> None:
    conn = connect()
    conn.execute("UPDATE files SET rel_dir=? WHERE id=?", (rel_dir or "", file_id))
    conn.commit()


def set_file_status(file_id: int, status: str, error: str | None = None,
                    chunk_count: int | None = None, meta: dict | None = None) -> None:
    conn = connect()
    fields = ["status=?", "error=?"]
    vals: list[Any] = [status, error]
    if chunk_count is not None:
        fields.append("chunk_count=?")
        vals.append(chunk_count)
    if meta is not None:
        import json as _json

        fields.append("meta=?")
        vals.append(_json.dumps(meta, ensure_ascii=False))
    if status == "ok":
        fields.append("indexed_at=?")
        vals.append(_now())
    vals.append(file_id)
    conn.execute(f"UPDATE files SET {','.join(fields)} WHERE id=?", vals)
    conn.commit()


def delete_file(file_id: int) -> None:
    conn = connect()
    ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
    for cid in ids:
        conn.execute("DELETE FROM chunks_tok WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks_gram WHERE rowid=?", (cid,))
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM files WHERE id=?", (file_id,))
    conn.commit()


def list_files(status: str | None = None, limit: int = 20000) -> list[dict]:
    conn = connect()
    sql = "SELECT * FROM files"
    args: list[Any] = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY indexed_at DESC, name COLLATE NOCASE LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def get_file(file_id: int) -> dict | None:
    conn = connect()
    r = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    return dict(r) if r else None


def file_counts() -> dict[str, int]:
    conn = connect()
    out = {"total": 0, "ok": 0, "error": 0, "pending": 0, "indexing": 0, "chunks": 0}
    for r in conn.execute("SELECT status, COUNT(*) c FROM files GROUP BY status"):
        out[r["status"]] = r["c"]
        out["total"] += r["c"]
    out["chunks"] = conn.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
    return out


# ---------------------------------------------------------------- 内容块

def replace_chunks(file_id: int, chunks: Iterable[dict]) -> int:
    """写入某文件的所有内容块（先清旧块）。"""
    conn = connect()
    old_ids = [r["id"] for r in conn.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
    for cid in old_ids:
        conn.execute("DELETE FROM chunks_tok WHERE rowid=?", (cid,))
        conn.execute("DELETE FROM chunks_gram WHERE rowid=?", (cid,))
    if old_ids:
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))

    n = 0
    for ch in chunks:
        cur = conn.execute(
            """INSERT INTO chunks(file_id,ord,loc_type,loc_label,loc_json,text,nchar)
               VALUES(?,?,?,?,?,?,?)""",
            (
                file_id,
                ch.get("ord", n),
                ch.get("loc_type", "text"),
                ch.get("loc_label", ""),
                ch.get("loc_json"),
                ch.get("text", ""),
                len(ch.get("text", "")),
            ),
        )
        cid = int(cur.lastrowid)
        _index_chunk(conn, cid, ch.get("text", ""))
        n += 1
    conn.commit()
    return n


def _index_chunk(conn: sqlite3.Connection, cid: int, text: str) -> None:
    from . import textutil

    toks = " ".join(textutil.tokenize(text))
    grams = " ".join(textutil.char_bigrams(text))
    conn.execute("INSERT INTO chunks_tok(rowid,tokens) VALUES(?,?)", (cid, toks))
    conn.execute("INSERT INTO chunks_gram(rowid,grams) VALUES(?,?)", (cid, grams))


def get_chunk(chunk_id: int) -> dict | None:
    conn = connect()
    r = conn.execute(
        """SELECT c.*, f.name AS file_name, f.path AS file_path, f.ext AS file_ext,
                  f.meta AS file_meta
           FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=?""",
        (chunk_id,),
    ).fetchone()
    return dict(r) if r else None


def list_chunks(file_id: int) -> list[dict]:
    conn = connect()
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM chunks WHERE file_id=? ORDER BY ord", (file_id,))
    ]


# ---------------------------------------------------------------- 问答历史

def add_history(question: str, answer: str, mode: str = "", degraded: bool = False,
                expanded: bool = False, file_id: int | None = None,
                file_name: str = "", citations: list | None = None,
                cited: list | None = None) -> int:
    """记下一轮问答。服务端自动记，客户端不记 —— 免得页面一崩历史就丢。"""
    import json as _json

    conn = connect()
    cur = conn.execute(
        """INSERT INTO qa_history(ts,question,answer,mode,degraded,expanded,
                                  file_id,file_name,citations,cited)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            _now(),
            question or "",
            answer or "",
            mode or "",
            1 if degraded else 0,
            1 if expanded else 0,
            file_id,
            file_name or "",
            _json.dumps(citations or [], ensure_ascii=False),
            _json.dumps(cited or [], ensure_ascii=False),
        ),
    )
    conn.commit()
    hid = int(cur.lastrowid)
    # 只留最近 500 条：历史表会一直长，不封顶的话索引库会慢慢膨胀
    conn.execute(
        "DELETE FROM qa_history WHERE id NOT IN "
        "(SELECT id FROM qa_history ORDER BY ts DESC LIMIT 500)"
    )
    conn.commit()
    return hid


def list_history(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM qa_history ORDER BY ts DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_history_row(r) for r in rows]


def count_history() -> int:
    conn = connect()
    return int(conn.execute("SELECT COUNT(*) c FROM qa_history").fetchone()["c"])


def get_history(hid: int) -> dict | None:
    conn = connect()
    r = conn.execute("SELECT * FROM qa_history WHERE id=?", (hid,)).fetchone()
    return _history_row(r) if r else None


def delete_history(hid: int) -> bool:
    conn = connect()
    cur = conn.execute("DELETE FROM qa_history WHERE id=?", (hid,))
    conn.commit()
    return cur.rowcount > 0


def clear_history() -> int:
    conn = connect()
    cur = conn.execute("DELETE FROM qa_history")
    conn.commit()
    return cur.rowcount


def _history_row(r: sqlite3.Row) -> dict:
    import json as _json

    d = dict(r)
    for key in ("citations", "cited"):
        try:
            d[key] = _json.loads(d.get(key) or "[]")
        except Exception:
            d[key] = []
    d["degraded"] = bool(d.get("degraded"))
    d["expanded"] = bool(d.get("expanded"))
    return d


# ---------------------------------------------------------------- 目录

def dir_scope(prefix: str, alias: str = "f") -> tuple[str, list]:
    """目录前缀过滤条件：**目录本身 + 它下面的所有子目录**。

    坑（别再写成只有 LIKE）：``rel_dir LIKE 'x/%'`` 匹配不到**直接放在 x 目录里**
    的文件 —— 它们的 rel_dir 恰好等于 ``x``（不带尾斜杠），只有子目录才是
    ``x/子目录``。于是「限定到 2026 目录」会把 2026 根下的文件全漏掉，
    只剩子目录里的文件。必须等值 + 子树一起匹配。

    返回 (SQL 片段, 参数列表)；prefix 为空表示不过滤。
    """
    p = (prefix or "").strip().strip("/")
    if not p:
        return "", []
    # 目录名里可能有 % 或 _，不转义会被当成通配符：
    # 限定「2026_Q1」会把「2026XQ1」也拉进来，范围限定就漏了。
    esc = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return (
        f" AND ({alias}.rel_dir = ? OR {alias}.rel_dir LIKE ? ESCAPE '\\')",
        [p, esc + "/%"],
    )


def list_folders() -> list[dict]:
    """按相对目录聚合已入库文件，供界面做目录树。

    rel_dir 为空表示文件直接放在资料根目录下。
    """
    conn = connect()
    rows = conn.execute(
        """SELECT COALESCE(rel_dir,'') AS d, COUNT(*) c
           FROM files WHERE status='ok'
           GROUP BY COALESCE(rel_dir,'') ORDER BY d"""
    ).fetchall()
    return [{"dir": r["d"], "files": int(r["c"])} for r in rows]


# ---------------------------------------------------------------- 根目录

def add_root(path: str, kind: str = "folder") -> int:
    conn = connect()
    conn.execute(
        "INSERT INTO roots(path,kind,added_at) VALUES(?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind",
        (path, kind, _now()),
    )
    conn.commit()
    r = conn.execute("SELECT id FROM roots WHERE path=?", (path,)).fetchone()
    return int(r["id"])


def list_roots() -> list[dict]:
    conn = connect()
    return [dict(r) for r in conn.execute("SELECT * FROM roots ORDER BY id")]


def remove_root(root_id: int) -> None:
    conn = connect()
    conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
    conn.commit()


def touch_root(root_id: int) -> None:
    conn = connect()
    conn.execute("UPDATE roots SET last_scan=? WHERE id=?", (_now(), root_id))
    conn.commit()


def _now() -> float:
    import time

    return time.time()
