"""FastAPI 本地服务：REST 接口 + 前端静态资源。"""
from __future__ import annotations

import json
import ipaddress
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, llm, qa, search as search_mod
from .indexer import IndexManager
from .parsers import SUPPORTED_EXTS, is_supported

app = FastAPI(title="EveryFind", docs_url=None, redoc_url=None)

WEB_DIR = config.resource_dir()
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

_mgr = IndexManager.instance()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    db.init_db()
    # 老库的目录列是空的，补上，否则界面按目录分组会把文件全堆在根目录下
    try:
        IndexManager.backfill_rel_dir()
    except Exception:
        pass
    _mgr.ensure_worker()
    _mgr.ensure_watcher()
    yield
    _mgr.stop()


app.router.lifespan_context = _lifespan


# ------------------------------------------------------------------ 跨站防护

@app.middleware("http")
async def _block_cross_site(request, call_next):
    """限制本机 Host，并校验浏览器 Origin 的协议、主机和端口。

    同时拒绝浏览器明确标记的跨站读取，防止其他网页探测本机资料或设置。
    """
    host = (request.url.hostname or '').lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if not loopback:
        return JSONResponse(status_code=403, content={"detail": "仅允许本机地址访问"})
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse(status_code=403, content={"detail": "已拒绝跨站请求"})
    origin = request.headers.get('origin')
    if origin:
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}".lower()
        if origin.lower() != expected:
            return JSONResponse(status_code=403, content={"detail": "已拒绝跨站请求"})
    return await call_next(request)


# ------------------------------------------------------------------ 页面

@app.get("/")
def index():
    f = WEB_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"error": "前端资源缺失"}, status_code=500)
    return FileResponse(str(f))


# ------------------------------------------------------------------ 配置

@app.get("/api/config")
def get_config():
    cfg = config.load_config()
    info = llm.describe(cfg)
    llm_cfg = dict(cfg.get("llm") or {})
    llm_cfg.pop('api_key', None)
    llm_cfg['has_api_key'] = info['has_api_key']
    cfg['llm'] = llm_cfg
    return {"config": cfg, "llm": info, "ocr_available": _ocr_available()}


def _ocr_available() -> bool:
    from .ocr import is_available

    return is_available()


@app.post("/api/config")
def set_config(payload: dict = Body(...)):
    cfg = config.load_config()
    incoming = payload or {}
    for key in ("llm", "ocr", "index", "retrieve", "ui"):
        if key in incoming and isinstance(incoming[key], dict):
            cfg[key] = {**(cfg.get(key) or {}), **incoming[key]}
    if incoming.get('clear_api_key') is True:
        cfg['llm']['api_key'] = ''
    try:
        saved = config.save_config(cfg)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    public = json.loads(json.dumps(saved))
    public['llm'].pop('api_key', None)
    public['llm']['has_api_key'] = bool(saved['llm'].get('api_key'))
    return {"config": public, "llm": llm.describe(saved)}


@app.post("/api/llm/test")
def llm_test():
    return llm.probe(config.load_config())


@app.get("/api/ollama/models")
def ollama_models(base_url: str = Query("http://127.0.0.1:11434")):
    return {"models": llm.test_ollama_models(base_url)}


# ------------------------------------------------------------------ 状态

@app.get("/api/status")
def status():
    counts = db.file_counts()
    return {
        "counts": counts,
        "progress": dict(_mgr.progress),
        "roots": db.list_roots(),
        "supported": SUPPORTED_EXTS,
        "sync_error": _mgr.last_error,
        "last_scan": _mgr.last_scan,
    }


# ------------------------------------------------------------------ 资料源

@app.get("/api/roots")
def get_roots():
    return {"roots": db.list_roots()}


@app.post("/api/roots")
def add_root(payload: dict = Body(...)):
    path = str((payload or {}).get("path") or "").strip()
    kind = str((payload or {}).get("kind") or "folder").strip()
    if not path:
        raise HTTPException(400, "缺少路径")
    p = Path(path)
    if not p.exists():
        raise HTTPException(400, f"路径不存在：{path}")
    root_id = db.add_root(str(p.resolve()), kind)
    files = _mgr.scan_root(str(p.resolve()), kind)
    added = _mgr.enqueue(files, kind, root=str(p.resolve()))
    return {"root_id": root_id, "path": str(p.resolve()), "kind": kind, "files": added}


@app.delete("/api/roots/{root_id}")
def del_root(root_id: int):
    db.remove_root(root_id)
    return {"ok": True}


@app.post("/api/rescan")
def rescan():
    stats = _mgr.rescan_roots()
    return {"ok": True, "stats": stats}


@app.post("/api/reindex")
def reindex():
    n = _mgr.reindex_all()
    return {"ok": True, "files": n}


@app.post("/api/upload")
async def upload(
    files: list[UploadFile] = File(...),
    paths: str = Form(""),
    dest: str = Form(""),
):
    """拖拽导入：把浏览器传过来的文件落盘到资料目录再索引。

    files 与 paths 一一对应，paths 是文件在拖入目录里的相对路径，
    用于还原文件夹结构；paths 缺失时按平铺文件处理。
    """
    base = (config.data_dir() / "uploads").resolve()
    sub = _safe_rel(dest)
    target = (base / sub) if sub else base
    try:
        target = target.resolve()
    except OSError:
        target = base
    if not _within(str(base), str(target)):
        target = base
    target.mkdir(parents=True, exist_ok=True)

    rel_list: list[str] = []
    if paths:
        try:
            parsed = json.loads(paths)
            if isinstance(parsed, list):
                rel_list = [str(x) for x in parsed]
        except Exception:
            rel_list = []

    saved: list[str] = []
    # 逐文件结果：前端要能只重试失败的那几个，而不是整批重来
    results: list[dict] = []
    for i, f in enumerate(files):
        rel = _safe_rel(rel_list[i]) if i < len(rel_list) else ""
        if not rel:
            rel = _safe_name(f.filename)
        out = target / rel
        try:
            out = out.resolve()
        except OSError:
            results.append({"name": f.filename or "", "rel": rel, "status": "error",
                            "detail": "路径无法解析"})
            continue
        if not _within(str(target), str(out)):
            results.append({"name": f.filename or "", "rel": rel, "status": "error",
                            "detail": "路径越界，已拒绝"})
            continue
        if not is_supported(out.name):
            results.append({"name": f.filename or "", "rel": rel, "status": "skipped",
                            "detail": "不支持的格式"})
            continue
        stem, suffix = out.stem, out.suffix
        i2 = 1
        while out.exists():
            out = out.with_name(f"{stem}({i2}){suffix}")
            i2 += 1
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("wb") as fh:
                shutil.copyfileobj(f.file, fh)
        except OSError as exc:
            results.append({"name": f.filename or "", "rel": rel, "status": "error",
                            "detail": f"写入失败：{exc}"})
            continue
        saved.append(str(out))
        results.append({"name": f.filename or "", "rel": rel, "status": "saved",
                        "detail": "", "path": str(out)})

    # rel_dir 一律相对 uploads 根目录，这样目录树不会因为 dest 传了子目录就分裂
    added = _mgr.enqueue(saved, "upload", root=str(base))
    return {"ok": True, "files": added, "paths": saved, "results": results}


# Windows 文件名非法字符 + 控制符
_ILLEGAL_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _within(base: str, target: str) -> bool:
    """判断 target 是否真的在 base 目录之内。

    不能用 str.startswith(base)：base=E:\\data\\uploads 时，
    E:\\data\\uploads_evil 也会通过。要带上路径分隔符再比。
    """
    try:
        b = os.path.normcase(os.path.abspath(base))
        t = os.path.normcase(os.path.abspath(target))
    except Exception:
        return False
    return t == b or t.startswith(b + os.sep)


def _safe_name(name: str) -> str:
    """把文件名清洗成 Windows 下合法的名字。"""
    s = _ILLEGAL_NAME.sub("_", str(name or "")).strip(" .")
    return s or "unnamed"


def _safe_rel(rel: str) -> str:
    """把相对路径清洗成安全的相对路径。

    去掉盘符、".."、空段等，防止写到资料目录之外。
    """
    s = str(rel or "").replace("\\", "/")
    parts: list[str] = []
    for seg in s.split("/"):
        seg = seg.strip()
        if not seg or seg in (".", ".."):
            continue
        if re.match(r"^[A-Za-z]:$", seg):
            continue
        parts.append(_safe_name(seg))
    return "/".join(parts)


# ------------------------------------------------------------------ 文件

@app.get("/api/files")
def list_files():
    rows = db.list_files()
    for r in rows:
        r.pop("meta", None)
    return {"files": rows}


@app.post("/api/files/{file_id}/reindex")
def reindex_file(file_id: int):
    if not _mgr.reindex_file(file_id):
        raise HTTPException(404, "文件不存在")
    return {"ok": True}


@app.delete("/api/files/{file_id}")
def remove_file(file_id: int, from_disk: bool = Query(False)):
    """从索引里移除一个文件。

    默认只删索引行，磁盘上的原文件不动 —— 它多半属于某个已添加的文件夹，
    下次自动扫描还会被重新索引回来。from_disk=true 才会连磁盘文件一起删。
    """
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "文件不存在")
    deleted = False
    if from_disk:
        try:
            p = Path(f["path"])
            if p.is_file():
                p.unlink()
                deleted = True
        except OSError as exc:
            raise HTTPException(500, f"删除文件失败：{exc}")
    db.delete_file(file_id)
    return {"ok": True, "deleted_from_disk": deleted}


@app.post("/api/open")
def open_file(payload: dict = Body(...)):
    try:
        file_id = int((payload or {}).get("file_id") or 0)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "file_id 必须是整数")
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "文件不存在")
    path = f["path"]
    if not Path(path).exists():
        raise HTTPException(400, "文件已不存在于磁盘")
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        raise HTTPException(500, f"打开失败：{exc}")
    return {"ok": True}


@app.post("/api/reveal")
def reveal_file(payload: dict = Body(...)):
    try:
        file_id = int((payload or {}).get("file_id") or 0)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "file_id 必须是整数")
    f = db.get_file(file_id)
    if not f:
        raise HTTPException(404, "文件不存在")
    path = f["path"]
    if not Path(path).exists():
        raise HTTPException(400, "文件已不存在于磁盘")
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as exc:
        raise HTTPException(500, f"定位失败：{exc}")
    return {"ok": True}


# ------------------------------------------------------------------ 检索

@app.get("/api/search")
def search(q: str = Query(""), file_id: int | None = Query(None), limit: int = Query(20, ge=1, le=200),
           dir: str = Query("")):
    hits = search_mod.search(q, limit=limit, file_id=file_id,
                             dir_prefix=(dir or None))
    for h in hits:
        h.pop("text", None)
    return {"query": q, "hits": hits}


@app.post("/api/ask")
def ask(payload: dict = Body(...)):
    payload = payload or {}
    question = str(payload.get("question") or "").strip()
    top_k = payload.get("top_k")
    prev = payload.get("prev_questions") or []
    file_id = payload.get("file_id")
    dir_prefix = str(payload.get("dir") or "").strip() or None
    # 参数不合法要返回 400，不能 int() 抛异常变成 500
    try:
        top_k = int(top_k) if top_k else None
    except (TypeError, ValueError):
        raise HTTPException(400, "top_k 必须是数字")
    try:
        file_id = int(file_id) if file_id else None
    except (TypeError, ValueError):
        raise HTTPException(400, "file_id 必须是数字")
    if isinstance(prev, str) or not isinstance(prev, (list, tuple)):
        prev = []
    cfg = config.load_config()
    res = qa.answer(
        question,
        cfg,
        top_k=top_k,
        prev_questions=[str(x) for x in prev][-5:],
        file_id=file_id,
        dir_prefix=dir_prefix,
    )
    # 历史由服务端记：页面崩了、用户刷新了，记录也不会丢。
    # 空问题不算一次问答（「请输入问题。」），别往历史里塞空白条目。
    if question:
        try:
            files = res.get("files") or []
            res["history_id"] = db.add_history(
                question,
                res.get("answer") or "",
                mode=res.get("mode") or "",
                degraded=bool(res.get("degraded")),
                expanded=bool(res.get("expanded")),
                file_id=file_id,
                file_name=(files[0].get("file_name") if files else "") or "",
                citations=res.get("citations") or [],
                cited=res.get("cited") or [],
            )
        except Exception as exc:
            # 写历史失败不能让整次提问失败，但也不能悄悄吞掉 ——
            # 否则用户会以为「历史功能坏了」却查不到任何线索
            res["history_id"] = 0
            res["history_error"] = str(exc)[:200]
            print(f"[everyfind] 问答历史写入失败：{exc}")
    else:
        res["history_id"] = 0
    return res


# ------------------------------------------------------------------ 问答历史

@app.get("/api/history")
def get_history(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    items = db.list_history(limit=limit, offset=offset)
    return {"items": items, "total": db.count_history()}


@app.delete("/api/history/{hid}")
def del_history(hid: int):
    if not db.delete_history(hid):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@app.delete("/api/history")
def clear_history():
    return {"ok": True, "removed": db.clear_history()}


@app.get("/api/history/export")
def export_history():
    """把全部问答历史导出成 Markdown。"""
    items = db.list_history(limit=100000)
    if not items:
        raise HTTPException(400, "还没有问答记录")
    lines = ["# EveryFind 问答记录", "", f"共 {len(items)} 条 · 导出于 {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for it in items:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it.get("ts") or 0))
        lines.append(f"## {it.get('question') or ''}")
        lines.append("")
        lines.append(f"> {stamp}　·　{it.get('mode') or ''}"
                     + ("　·　限定文件：" + it["file_name"] if it.get("file_name") else ""))
        lines.append("")
        for ln in (it.get("answer") or "").split("\n"):
            if ln.strip():
                lines.append(ln.strip())
        lines.append("")
    body = "\n".join(lines).encode("utf-8")
    return Response(content=body, media_type="text/markdown; charset=utf-8",
                    headers=_export_headers("md"))


# ------------------------------------------------------------------ 目录

@app.get("/api/folders")
def folders():
    """资料库的目录结构（相对根目录），供界面按目录浏览。"""
    items = db.list_folders()
    return {"folders": items, "count": len(items)}


@app.post("/api/resolve")
def resolve(payload: dict = Body(...)):
    """只检索、不调模型：把问题解析成带编号的引用列表。

    点开旧回答里的引用时用它兜底，不会白白再调一次云端模型。
    """
    payload = payload or {}
    question = str(payload.get("question") or "")
    file_id = payload.get("file_id")
    try:
        file_id = int(file_id) if file_id else None
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "file_id 必须是整数")
    cites = qa.resolve_citations(
        question,
        config.load_config(),
        file_id=file_id,
    )
    return {"citations": cites}


@app.get("/api/report")
def report():
    """索引体检报告：哪些资料进来了、哪些没进来、为什么。

    这些信息索引库里本来就有（状态 / 错误原因 / 块数），但界面上看不到，
    用户只能看到「N 个文件」而不知道漏了什么。
    """
    conn = db.connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM files")]
    counts = db.file_counts()

    by_ext: dict[str, dict] = {}
    problems: list[dict] = []

    for r in rows:
        ext = (r.get("ext") or "").lower() or "(无扩展名)"
        slot = by_ext.setdefault(ext, {"ext": ext, "files": 0, "chunks": 0, "failed": 0})
        slot["files"] += 1
        slot["chunks"] += int(r.get("chunk_count") or 0)

        try:
            meta = json.loads(r.get('meta') or '{}')
        except (TypeError, ValueError):
            meta = {}
        for detail in meta.get('ocr_errors', []):
            problems.append({'level': 'warn', 'kind': '部分页面 OCR 失败',
                             'name': r.get('name') or '', 'path': r.get('path') or '', 'detail': detail})
        status = r.get("status")
        name = r.get("name") or ""
        path = r.get("path") or ""
        if status == "error":
            slot["failed"] += 1
            problems.append({
                "level": "error", "kind": "解析失败", "name": name, "path": path,
                "detail": r.get("error") or "未知原因",
            })
        elif status == "skipped":
            problems.append({
                "level": "warn", "kind": "已跳过", "name": name, "path": path,
                "detail": r.get("error") or "超过单文件大小上限",
            })
        elif status in ("pending", "indexing"):
            problems.append({
                "level": "info", "kind": "待索引" if status == "pending" else "索引中",
                "name": name, "path": path, "detail": "",
            })
        elif status == "ok" and int(r.get("chunk_count") or 0) == 0:
            problems.append({
                "level": "warn", "kind": "内容为空", "name": name, "path": path,
                "detail": "索引成功但没有抽到任何文字（可能是空文件或纯图片）",
            })

    # 体检提示：配置层面的问题
    health: list[str] = []
    cfg = config.load_config()
    if not _ocr_available():
        health.append("本机未检测到 OCR 引擎，图片与扫描件无法识别文字。")
    idx = cfg.get("index") or {}
    if int(idx.get("auto_rescan_seconds", 60) or 0) <= 0:
        health.append("自动同步已关闭，新增文件需要手动点左侧「刷新」才会入库。")
    if not (cfg.get("llm") or {}).get("api_key") and (cfg.get("llm") or {}).get("provider") == "openai":
        health.append("已选择 OpenAI 兼容接口但没有填 API Key，提问会退回原文摘录模式。")
    if not counts.get("ok"):
        health.append("目前没有任何索引成功的文件，先添加一个资料来源。")

    return {
        "summary": {
            "total": counts.get("total", 0),
            "ok": counts.get("ok", 0),
            "error": counts.get("error", 0),
            "skipped": counts.get("skipped", 0),
            "pending": counts.get("pending", 0),
            "indexing": counts.get("indexing", 0),
            "chunks": counts.get("chunks", 0),
            "problems": len(problems),
        },
        "by_ext": sorted(by_ext.values(), key=lambda x: (-x["files"], x["ext"])),
        "problems": problems[:500],
        "health": health,
        "roots": db.list_roots(),
    }


@app.get("/api/source/{chunk_id}")
def source(chunk_id: int, question: str = Query("")):
    d = qa.source_detail(chunk_id, question)
    if not d:
        raise HTTPException(404, "来源不存在")
    return d


# ------------------------------------------------------------------ 导出

def _export_stem() -> str:
    return "EveryFind-问答-" + time.strftime("%Y%m%d-%H%M%S")


def _export_headers(ext: str) -> dict:
    """下载头。

    HTTP 头只能编码成 latin-1，中文文件名直接放进去会抛
    UnicodeEncodeError。所以：给一个纯 ASCII 的兜底名，
    再用 RFC 5987 的 filename* 带 UTF-8 真名。
    """
    ascii_name = "EveryFind-export-" + time.strftime("%Y%m%d-%H%M%S") + "." + ext
    quoted = urllib.parse.quote(_export_stem() + "." + ext)
    return {
        "Content-Disposition":
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
    }


def _export_md(question: str, answer_text: str, citations: list[dict],
               mode: str, generated: str) -> str:
    lines: list[str] = []
    lines.append("# " + (question or "（未命名问题）"))
    lines.append("")
    lines.append(f"> 由 EveryFind 生成于 {generated}　|　"
                 + ("模型生成 · 已标注引用来源" if mode == "llm" else "本地原文摘录（未接入模型）"))
    lines.append("")
    lines.append("## 回答")
    lines.append("")
    lines.append(answer_text.strip())
    lines.append("")
    if citations:
        lines.append("## 引用出处")
        lines.append("")
        for c in citations:
            loc = c.get("loc_label") or ""
            lines.append(f"- **[{c.get('n')}]** {c.get('file_name')}　·　{loc}")
            snippet = (c.get("snippet") or "").replace("\n", " ").strip()
            if snippet:
                lines.append(f"  > {snippet}")
        lines.append("")
    return "\n".join(lines)


def _export_docx(question: str, answer_text: str, citations: list[dict],
                 mode: str, generated: str) -> bytes:
    import io

    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)

    doc.add_heading(question or "（未命名问题）", level=1)
    p = doc.add_paragraph()
    r = p.add_run(
        f"由 EveryFind 生成于 {generated}　|　"
        + ("模型生成 · 已标注引用来源" if mode == "llm" else "本地原文摘录（未接入模型）")
    )
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x8A, 0x97, 0x8F)

    doc.add_heading("回答", level=2)
    for line in (answer_text or "").strip().split("\n"):
        if line.strip():
            doc.add_paragraph(line.strip())

    if citations:
        doc.add_heading("引用出处", level=2)
        for c in citations:
            doc.add_paragraph(f"[{c.get('n')}] {c.get('file_name')}　·　{c.get('loc_label')}")
            snippet = (c.get("snippet") or "").replace("\n", " ").strip()
            if snippet:
                q = doc.add_paragraph(snippet)
                q.paragraph_format.left_indent = Pt(14)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@app.post("/api/export")
def export(payload: dict = Body(...)):
    """把一次问答导出成 Markdown / Word（带引用出处附录）。"""
    payload = payload or {}
    fmt = str(payload.get("format") or "md").lower()
    question = str(payload.get("question") or "")
    answer_text = str(payload.get("answer") or "")
    mode = str(payload.get("mode") or "")
    citations = payload.get("citations") or []
    citations = [c for c in citations if isinstance(c, dict)]
    generated = time.strftime("%Y-%m-%d %H:%M:%S")

    if fmt == "docx":
        try:
            data = _export_docx(question, answer_text, citations, mode, generated)
        except Exception as exc:
            raise HTTPException(500, f"导出 Word 失败：{exc}")
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers=_export_headers("docx"),
        )

    text = _export_md(question, answer_text, citations, mode, generated)
    return Response(
        content=text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers=_export_headers("md"),
    )


# ------------------------------------------------------------------ 文件系统浏览

@app.get("/api/fs/list")
def fs_list(path: str = Query("")):
    """本地文件系统浏览，供「选择文件夹 / 选择文件」使用。"""
    p = Path(path) if path else None
    result: dict[str, Any] = {"path": str(p.resolve()) if p else "", "parent": "", "dirs": [], "files": []}

    if p is None or not p.exists():
        # 列出可用盘符
        drives = []
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                d = f"{letter}:\\"
                if os.path.exists(d):
                    drives.append({"name": d, "path": d})
        else:
            drives.append({"name": "/", "path": "/"})
        result["drives"] = drives
        result["need_drive"] = True
        return result

    if p.is_file():
        p = p.parent

    try:
        for entry in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if entry.name.startswith("$") or entry.name in ("System Volume Information",):
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                result["dirs"].append({"name": entry.name, "path": str(entry)})
            elif is_supported(entry):
                result["files"].append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        result["error"] = "无权访问该目录"

    parent = p.parent
    result["parent"] = str(parent) if str(parent) != str(p) else ""
    result["name"] = p.name or str(p)
    return result
