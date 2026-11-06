"""纯文本类文件 (.txt/.md/.csv/.json/.xml/.log 等) 解析。"""
from __future__ import annotations

import codecs
from pathlib import Path

from .base import Chunk, ParseError

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".log", ".rst", ".py", ".js", ".ts", ".java", ".c",
    ".cpp", ".h", ".sql", ".html", ".htm",
}


def _plausible(text: str) -> float:
    """一段文本「像不像人话」的比例，用来区分 UTF-16 的两种字节序。"""
    if not text:
        return 0.0
    n = min(len(text), 4000)
    good = 0
    for ch in text[:n]:
        o = ord(ch)
        if (0x20 <= o <= 0x7E) or (0x4E00 <= o <= 0x9FFF) or (0x3000 <= o <= 0x303F) \
                or (0xFF00 <= o <= 0xFFEF) or ch in "\r\n\t":
            good += 1
    return good / n


def _detect_utf16(raw: bytes) -> str | None:
    """判断无 BOM 的 UTF-16 字节序，返回 'utf-16-le' / 'utf-16-be' / None。

    注意：UTF-16 必须排在 GB18030 之前判断。GB18030 能「成功」解码
    UTF-16 的字节（只是解出来是乱码），一旦它先命中，后面的 utf-16
    分支就永远走不到。
    """
    if len(raw) < 4 or len(raw) % 2:
        return None
    even_nul = sum(1 for i in range(0, len(raw) - 1, 2) if raw[i] == 0)
    odd_nul = sum(1 for i in range(1, len(raw), 2) if raw[i] == 0)
    pairs = len(raw) // 2
    # ASCII 字符在 UTF-16 里占两字节、其中一个恒为 0：
    # LE 时 NUL 集中在奇数位，BE 时集中在偶数位。
    if even_nul + odd_nul >= pairs * 0.25:
        return "utf-16-le" if odd_nul >= even_nul else "utf-16-be"
    # 纯中文的 UTF-16 一个 NUL 都没有，只能靠「解出来像不像人话」判断
    le = _plausible(raw.decode("utf-16-le", errors="replace"))
    be = _plausible(raw.decode("utf-16-be", errors="replace"))
    if le >= 0.95 and le > be * 1.2:
        return "utf-16-le"
    if be >= 0.95 and be > le * 1.2:
        return "utf-16-be"
    return None


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if not raw:
        return ""

    # 1) BOM 优先
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass

    # 2) 严格 UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 3) 无 BOM 的 UTF-16（Windows 导出的 CSV/TXT 很常见）
    enc = _detect_utf16(raw)
    if enc:
        return raw.decode(enc, errors="replace")

    # 4) 中文 / 繁体
    #    GB18030 能解码绝大多数双字节序列（含 Big5），但解出来可能是
    #    私用区/生僻字，这时改用 Big5 更准。
    try:
        g = raw.decode("gb18030")
        if _plausible(g) >= 0.9:
            return g
        try:
            b = raw.decode("big5")
        except UnicodeDecodeError:
            return g
        return b if _plausible(b) > _plausible(g) else g
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("big5")
    except UnicodeDecodeError:
        pass

    # 5) 兜底
    return raw.decode("utf-8", errors="replace")


def parse(path: Path, opts: dict | None = None) -> tuple[list[Chunk], dict]:
    try:
        content = read_text(path)
    except Exception as exc:
        raise ParseError(f"无法读取文本文件: {exc}") from exc

    lines = content.splitlines()
    chunks: list[Chunk] = []
    ord_ = 0

    buf: list[str] = []
    buf_start = 1
    target = 700

    def flush(end_line: int) -> None:
        nonlocal ord_, buf, buf_start
        text = "\n".join(buf).strip()
        if text:
            chunks.append(
                Chunk(
                    ord=ord_,
                    loc_type="line",
                    loc_label=f"第 {buf_start}-{end_line} 行",
                    text=text,
                    loc={"kind": "lines", "start_line": buf_start, "end_line": end_line},
                )
            )
            ord_ += 1
        buf = []

    for i, line in enumerate(lines, 1):
        if not buf:
            buf_start = i
        buf.append(line)
        if sum(len(x) for x in buf) >= target:
            flush(i)
    if buf:
        flush(len(lines))

    meta = {"lines": len(lines), "chars": len(content)}
    return chunks, meta
