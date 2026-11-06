"""索引器：扫描资料来源 → 解析 → 写入 SQLite，支持增量更新与后台进度。"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable

from . import db
from .config import load_config
from .parsers import SKIP_DIRS, is_supported, parse_file

MAX_SCAN_DEPTH = 12


def _rel_dir(path: str, root: str | None) -> str:
    """文件相对于资料根目录的目录（正斜杠）。

    绝对路径既没用又会泄露本机结构，界面上只显示相对目录。
    根目录本身是文件、或文件不在根目录下时返回空串。
    """
    if not root:
        return ""
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except Exception:
        return ""
    if r.is_file():
        return ""
    try:
        rel = p.parent.relative_to(r)
    except ValueError:
        return ""
    s = rel.as_posix()
    return "" if s == "." else s


def _same_dir(path: str, root: str | None) -> bool:
    """文件是否就放在根目录这一层（rel_dir 为空但归属该根）。"""
    if not root:
        return False
    try:
        return Path(path).resolve().parent == Path(root).resolve()
    except Exception:
        return False


class IndexManager:
    """单例后台索引器。"""

    _instance: "IndexManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, str]] = deque()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_stop = threading.Event()
        # 自动同步的最近一次错误。吞掉异常会让「同步坏了」变得完全无感，
        # 所以把错误记下来，由 /api/status 暴露给界面。
        self.last_error = ""
        self.last_scan = 0.0
        self.progress: dict = {
            "state": "idle",       # idle|running|paused
            "current": "",
            "done": 0,
            "total": 0,
            "added": 0,
            "skipped": 0,
            "failed": 0,
            "message": "",
        }

    @classmethod
    def instance(cls) -> "IndexManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------ 队列

    def enqueue(self, paths: Iterable[str | Path], source: str = "file",
                root: str | None = None) -> int:
        n = 0
        for p in paths:
            p = str(Path(p).resolve())
            if not is_supported(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            db.upsert_file(p, Path(p).name, Path(p).suffix.lower(), st.st_size, st.st_mtime,
                           source, _rel_dir(p, root))
            with self._lock:
                self._queue.append((p, source))
                self.progress["total"] += 1
            n += 1
        self.ensure_worker()
        return n

    def ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="everyfind-indexer", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------ 自动同步
    def ensure_watcher(self) -> None:
        """启动后台重扫线程，实现"往资料文件夹丢新文件 → 自动入库"。

        文件夹根目录按 index.auto_rescan_seconds 扫（默认 60 秒）；
        整盘根目录走 os.walk，代价高得多，按 index.auto_rescan_disk_seconds 扫（默认 30 分钟）。
        设为 0 即关闭自动同步，只能手动点「刷新」。

        线程常驻，间隔每轮重读配置，所以改设置后不用重启就能生效。
        """
        if self._watch_thread and self._watch_thread.is_alive():
            return
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            name="everyfind-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def _watch_loop(self) -> None:
        next_fast = 0.0
        next_slow = 0.0
        while not self._watch_stop.wait(5):
            cfg = load_config().get("index") or {}
            fast = int(cfg.get("auto_rescan_seconds", 60) or 0)
            slow = int(cfg.get("auto_rescan_disk_seconds", 1800) or 0)
            now = time.time()
            kinds: set[str] = set()
            if fast > 0 and now >= next_fast:
                kinds.update({"folder", "file"})
                next_fast = now + max(fast, 5)
            if slow > 0 and now >= next_slow:
                kinds.add("disk")
                next_slow = now + max(slow, 60)
            if not kinds:
                continue
            with self._lock:
                busy = bool(self._queue) or self.progress.get("state") == "running"
            if busy:
                # 正在索引就跳过这一轮，等下一轮
                continue
            try:
                self.rescan_roots(kinds=kinds)
                with self._lock:
                    self.last_error = ""
                    self.last_scan = time.time()
            except Exception as exc:
                with self._lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self._stop.set()
        self._watch_stop.set()

    def wait_idle(self, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                empty = not self._queue
            if empty and self.progress.get("state") != "running":
                return True
            time.sleep(0.15)
        return False

    # ------------------------------------------------------------ 主循环

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                item = self._queue.popleft() if self._queue else None
                if item is None:
                    self.progress['state'] = 'idle'
                    self.progress['current'] = ''
                else:
                    self.progress['state'] = 'running'
                    self.progress['current'] = Path(item[0]).name
            if item is None:
                # 保持同一工作线程，消除旧线程退出与新任务入队之间的竞态。
                self._stop.wait(0.1)
                continue
            path, source = item
            try:
                self._index_one(path, source)
            except Exception as exc:  # 单文件失败不影响整体
                with self._lock:
                    self.progress["failed"] += 1
                try:
                    fid = db.connect().execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                    if fid:
                        db.set_file_status(int(fid["id"]), "error", str(exc)[:400])
                except Exception:
                    pass
            finally:
                with self._lock:
                    self.progress["done"] += 1
                    if self.progress["done"] >= self.progress["total"]:
                        self.progress["total"] = 0
                        self.progress["done"] = 0
                        self.progress["state"] = "idle"

    def _index_one(self, path: str, source: str) -> None:
        conn = db.connect()
        row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if not row:
            return
        file_id = int(row["id"])
        st = os.stat(path)
        cfg = load_config()

        # 增量：内容未变则跳过
        if row["status"] == "ok" and abs((row["mtime"] or 0) - st.st_mtime) < 1e-6 and row["size"] == st.st_size:
            with self._lock:
                self.progress["skipped"] += 1
            return

        max_mb = float(cfg.get("index", {}).get("max_file_mb", 120))
        if st.st_size > max_mb * 1024 * 1024:
            db.set_file_status(file_id, "skipped", f"文件超过 {max_mb:.0f} MB，已跳过")
            with self._lock:
                self.progress["skipped"] += 1
            return

        db.set_file_status(file_id, "indexing")
        opts = {
            "ocr_enabled": bool(cfg.get("ocr", {}).get("enabled", True)),
            "ocr_scanned_pdf": bool(cfg.get("ocr", {}).get("ocr_scanned_pdf", True)),
            "ocr_min_confidence": float(cfg.get("ocr", {}).get("min_confidence", 0.45)),
        }
        chunks, meta = parse_file(Path(path), opts)
        db.replace_chunks(file_id, [c.to_dict() for c in chunks])
        db.set_file_status(file_id, "ok", None, len(chunks), meta)
        with self._lock:
            self.progress["added"] += 1

    # ------------------------------------------------------------ 扫描

    def scan_root(self, root: str, kind: str = "folder",
                  progress_cb: Callable[[int, int, str], None] | None = None) -> list[str]:
        """扫描一个根目录，返回待索引文件列表。"""
        root_path = Path(root)
        found: list[str] = []
        if kind == "disk" or root_path.is_dir():
            found = self._walk(root_path, max_depth=MAX_SCAN_DEPTH)
        elif root_path.is_file():
            found = [str(root_path)]
        if progress_cb:
            progress_cb(len(found), len(found), root)
        return found

    def _walk(self, root: Path, max_depth: int = MAX_SCAN_DEPTH) -> list[str]:
        out: list[str] = []
        root_s = str(root).lower()
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda e: None):
            depth = len(Path(dirpath).relative_to(root).parts) if dirpath != str(root) else 0
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() not in SKIP_DIRS
                and not d.startswith("$")
                and d.lower() not in ("windows", "program files", "program files (x86)")
                and not (len(d) > 1 and d.startswith(".") and d.lower() not in (".github",))
            ]
            for fn in filenames:
                if fn.startswith("~$") or fn.startswith("."):
                    continue
                p = os.path.join(dirpath, fn)
                if is_supported(p):
                    out.append(p)
        return out

    def rescan_roots(self, incremental: bool = True, kinds: set[str] | None = None) -> dict:
        """重新扫描所有已添加的根目录，处理新增/修改/删除。

        kinds 限定只扫哪类根目录（folder/file/disk）；None 表示全扫。
        """
        stats = {"added": 0, "updated": 0, "removed": 0, "scanned": 0}
        conn = db.connect()
        for root in db.list_roots():
            if kinds and (root["kind"] or "folder") not in kinds:
                continue
            rpath = root["path"]
            if not Path(rpath).exists():
                continue
            files = self.scan_root(rpath, root["kind"])
            stats["scanned"] += len(files)
            paths = {os.path.normcase(os.path.abspath(f)) for f in files}
            # 清理已删除的文件
            for k in self._files_under(conn, rpath):
                if os.path.normcase(os.path.abspath(k["path"])) not in paths:
                    db.delete_file(int(k["id"]))
                    stats["removed"] += 1
            for f in files:
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                row = conn.execute("SELECT * FROM files WHERE path=?", (f,)).fetchone()
                if row is None:
                    stats["added"] += 1
                elif row["status"] == "ok" and row["size"] == st.st_size and abs((row["mtime"] or 0) - st.st_mtime) < 1e-6:
                    continue
                else:
                    stats["updated"] += 1
                db.upsert_file(f, Path(f).name, Path(f).suffix.lower(), st.st_size, st.st_mtime,
                               root["kind"], _rel_dir(f, rpath))
                with self._lock:
                    self._queue.append((f, root["kind"]))
                    self.progress["total"] += 1
            db.touch_root(int(root["id"]))
        stats["removed"] += self._prune_missing_uploads(conn)
        self.ensure_worker()
        return stats

    @staticmethod
    def _prune_missing_uploads(conn) -> int:
        """清理用户已从 uploads 目录删掉的文件。

        uploads 不是已注册的根目录，走不到上面的通用清理逻辑，
        这里单独兜一次底：磁盘上文件没了就把索引行一并删掉，
        否则检索还能搜到已经删掉的资料。
        """
        n = 0
        rows = conn.execute("SELECT id, path FROM files WHERE source='upload'").fetchall()
        for row in rows:
            try:
                alive = os.path.exists(row["path"])
            except OSError:
                alive = False
            if not alive:
                db.delete_file(int(row["id"]))
                n += 1
        return n

    @staticmethod
    def backfill_rel_dir() -> int:
        """给老库补 rel_dir。

        目录列是后加的，老用户的 index.db 里这一列全是空。
        不补的话，界面按目录分组会把所有文件都堆在根目录下面。
        """
        conn = db.connect()
        roots = sorted(db.list_roots(), key=lambda r: len(r["path"] or ""), reverse=True)
        if not roots:
            return 0
        n = 0
        rows = conn.execute(
            "SELECT id, path, source FROM files WHERE rel_dir IS NULL OR rel_dir=''"
        ).fetchall()
        for r in rows:
            if (r["source"] or "") == "upload":
                continue
            for root in roots:
                rd = _rel_dir(r["path"], root["path"])
                if rd or _same_dir(r["path"], root["path"]):
                    db.set_rel_dir(int(r["id"]), rd)
                    n += 1
                    break
        return n

    def reindex_file(self, file_id: int) -> bool:
        f = db.get_file(file_id)
        if not f:
            return False
        db.set_file_status(file_id, "pending")
        with self._lock:
            self._queue.append((f["path"], f["source"] or "file"))
            self.progress["total"] += 1
        self.ensure_worker()
        return True

    def reindex_all(self) -> int:
        conn = db.connect()
        rows = conn.execute("SELECT id FROM files WHERE status IN ('ok','error','skipped')").fetchall()
        for r in rows:
            self.reindex_file(int(r["id"]))
        return len(rows)

    @staticmethod
    def _files_under(conn, root_path: str) -> list[dict]:
        """取出位于 root_path 下的所有已入库文件。

        这里不能用 SQL LIKE 直接拼路径：Windows 下库里存的是反斜杠路径，
        而路径里还可能出现 % 或 _，LIKE 转义很容易出错，结果就是一条都匹配不上。
        改为先按盘符粗筛（盘符不含 %/_，无需转义），再用规范化路径精确判断归属。
        """
        raw = str(root_path)
        root_n = os.path.normcase(os.path.abspath(raw))
        # 根目录本身就是一个文件时（"选择文件"加进来的），按等值匹配；
        # 文件已被删掉时 os.path.isfile 为 False，靠扩展名兜底判断
        is_file = os.path.isfile(raw) or (
            not os.path.isdir(raw) and bool(os.path.splitext(raw)[1])
        )
        prefix = root_n.rstrip("\\/") + os.sep
        drive = os.path.splitdrive(raw)[0]
        if drive:
            rows = conn.execute(
                "SELECT id, path FROM files WHERE path LIKE ?", (drive + "%",)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, path FROM files").fetchall()
        out: list[dict] = []
        for r in rows:
            p = os.path.normcase(os.path.abspath(str(r["path"])))
            if is_file:
                hit = p == root_n
            else:
                hit = p.startswith(prefix)
            if hit:
                out.append(dict(r))
        return out
