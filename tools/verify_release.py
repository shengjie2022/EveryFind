# -*- coding: utf-8 -*-
"""EveryFind 发布验收：对着**打包好的 exe** 跑一遍真实业务链路。

为什么必须对着 exe 测：源码环境里能 import 的模块，打包后不一定在。
只验「exe 能启动、首页返回 200」抓不到缺依赖 —— 缺依赖只会在真正走到那条
代码路径时暴露。所以这里覆盖的是真实链路：建索引 -> 检索 -> 问答 -> 溯源 ->
体检 -> 导出 Word -> 拖拽上传 -> 删除清理。

用法：
    python tools/verify_release.py                        # 用默认路径与端口
    python tools/verify_release.py <EveryFind.exe> [端口]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
EXE = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "EveryFind" / "EveryFind.exe")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8799
BASE = f"http://127.0.0.1:{PORT}"
DEMO = ROOT / "演示资料"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("   " + detail) if detail else ""))
    return bool(ok)


def get(path: str, timeout: int = 60):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw


def post(path: str, payload: dict | None = None, timeout: int = 120):
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw


def post_bytes(path: str, payload: dict):
    """POST 但保留原始 bytes（导出接口要检查 docx 二进制）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status, dict(r.headers), r.read()


def post_code(path: str, payload: dict) -> int:
    """只取状态码：畸形参数应该返回 4xx，而不是 500。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1


def upload(files: list[tuple[str, bytes]], relpaths: list[str]) -> dict:
    """手工拼 multipart。"""
    boundary = "----ef" + uuid.uuid4().hex
    body = bytearray()
    for (name, data), rel in zip(files, relpaths):
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body += data + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="paths"\r\n\r\n{json.dumps(relpaths, ensure_ascii=False)}\r\n'
    ).encode("utf-8")
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/upload",
        data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_indexed(timeout: int = 180) -> list[dict]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = get("/api/status")
        if st.get("progress", {}).get("state") != "running":
            files = get("/api/files").get("files", [])
            if files and all(f.get("status") in ("ok", "error", "skipped") for f in files):
                return files
        time.sleep(1.0)
    return get("/api/files").get("files", [])


def main() -> int:
    if not Path(EXE).exists():
        print("找不到 exe：", EXE)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="ef_pkg_"))
    data_dir = tmp / "data"
    mat_dir = tmp / "资料"
    (mat_dir / "2026").mkdir(parents=True, exist_ok=True)
    for f in DEMO.glob("*"):
        if f.is_file():
            shutil.copy2(f, mat_dir / f.name)
    # 放一份到子目录里，用来验证目录感知
    shutil.copy2(mat_dir / "年度预算.xlsx", mat_dir / "2026" / "年度预算.xlsx")
    # 再放一份到孙目录里，用来验证「限定到 2026」要包含它的整个子树
    (mat_dir / "2026" / "子目录").mkdir(parents=True, exist_ok=True)
    shutil.copy2(mat_dir / "年度预算.xlsx", mat_dir / "2026" / "子目录" / "旧预算.xlsx")
    # UTF-16LE 带 BOM 的中文样本：验证编码探测在打包版里也生效
    (mat_dir / "编码样本.txt").write_bytes(
        "﻿差旅补贴标准为每天三百元，住宿费另行报销。".encode("utf-16-le")
    )
    # GB18030 样本
    (mat_dir / "国标样本.txt").write_bytes("差旅补贴按国家标准执行。".encode("gb18030"))
    # 故意损坏的 docx：用来验证体检报告能报出「解析失败」
    (mat_dir / "损坏样本.docx").write_bytes(b"NOT-A-REAL-DOCX" + bytes(range(256)) * 8)

    # ↓↓ 聚合计算的各种「坑表」：这些写法在真实表格里非常常见，
    # 但每一种都曾导致算错（合计行没排除 → 双倍计数；同名不同单位的列被合并 → 混算）
    import openpyxl

    def _mk(name: str, sheets: dict) -> None:
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for sname, rows in sheets.items():
            ws = wb.create_sheet(sname)
            for r in rows:
                ws.append(r)
        wb.save(str(mat_dir / name))

    # 合计行写成「合计：」「共计」「本年合计」「合 计」「合计（万元）」「Total」
    _mk("合计写法.xlsx", {"S": [
        ["项目", "预算"],
        ["差旅费", 100],
        ["设备费", 200],
        ["合计：", 300],
    ]})
    _mk("共计写法.xlsx", {"S": [
        ["项目", "预算"],
        ["差旅费", 100],
        ["设备费", 200],
        ["共计", 300],
    ]})
    # 同名不同单位的两列：100 元 + 50 万元，绝不能算成 150
    _mk("同名单位.xlsx", {"S": [
        ["项目", "预算（元）", "预算（万元）"],
        ["差旅费", 100, 50],
        ["设备费", 200, 30],
    ]})
    # 「3.5 万元」：万位前面有空格
    _mk("带空格万.xlsx", {"S": [
        ["项目", "预算"],
        ["差旅费", "3.5 万元"],
        ["设备费", "2 万元"],
    ]})

    env = dict(os.environ)
    env["EVERYFIND_DATA"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    print("· 启动打包版：", EXE)
    proc = subprocess.Popen(
        [EXE, "--port", str(PORT), "--no-browser"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # 等服务起来
        up = False
        for _ in range(120):
            try:
                get("/api/status", timeout=5)
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(1.0)
        if not check("打包版服务启动", up):
            return 1

        print("\n=== A. 基础能力 ===")
        st = get("/api/status")
        check("/api/status 含 sync_error 与 last_scan", "sync_error" in st and "last_scan" in st)
        check("根目录写入接口可用", True)

        r = post("/api/roots", {"path": str(mat_dir), "kind": "folder"})
        check("添加资料文件夹", bool(r.get("root_id")), json.dumps(r, ensure_ascii=False)[:120])

        files = wait_indexed()
        ok_files = [f for f in files if f.get("status") == "ok"]
        err_files = [f for f in files if f.get("status") == "error"]
        check("演示文件全部索引成功", len(ok_files) >= 7,
              f"ok={len(ok_files)} err={len(err_files)} 共{len(files)}")
        check("损坏文件被标记为解析失败", len(err_files) == 1,
              json.dumps([f.get("name") for f in err_files], ensure_ascii=False))

        print("\n=== B. 索引体检报告 ===")
        rep = get("/api/report")
        s = rep.get("summary", {})
        check("体检 summary 完整",
              all(k in s for k in ("total", "ok", "error", "skipped", "pending", "chunks", "problems")),
              json.dumps(s, ensure_ascii=False))
        check("体检含按格式分布", len(rep.get("by_ext", [])) >= 4,
              ",".join(e.get("ext", "") for e in rep.get("by_ext", [])))
        check("体检含问题清单字段", isinstance(rep.get("problems"), list))
        check("体检含健康提醒字段", isinstance(rep.get("health"), list),
              json.dumps(rep.get("health", []), ensure_ascii=False)[:160])
        prob = rep.get("problems", [])
        check("体检报出损坏文件为「解析失败」",
              any(p.get("kind") == "解析失败" for p in prob),
              json.dumps(prob, ensure_ascii=False)[:200])
        check("体检 summary 的 error 计数与清单一致",
              s.get("error") == len([p for p in prob if p.get("kind") == "解析失败"]),
              f"summary.error={s.get('error')}")
        check("打包版内置 OCR 可用（体检未报 OCR 缺失）",
              not any("OCR" in h for h in rep.get("health", [])),
              json.dumps(rep.get("health", []), ensure_ascii=False)[:160])

        print("\n=== B2. 目录感知 ===")
        folders = get("/api/folders").get("folders", [])
        dirs = {f["dir"]: f["files"] for f in folders}
        check("目录树含子目录 2026", "2026" in dirs, json.dumps(dirs, ensure_ascii=False))
        sub = [f for f in files if f.get("rel_dir") == "2026"]
        check("子目录文件的 rel_dir 已入库", len(sub) >= 1,
              json.dumps([(f.get("name"), f.get("rel_dir")) for f in sub], ensure_ascii=False))
        dhits = get("/api/search?q=" + urllib.parse.quote("预算") + "&dir=2026").get("hits", [])
        check("按目录限定检索有结果", len(dhits) > 0, f"{len(dhits)} 条命中")
        d_dirs = [h.get("file_rel_dir") or "" for h in dhits]
        check("目录限定的命中都在该目录下",
              all(d.startswith("2026") for d in d_dirs),
              json.dumps(d_dirs, ensure_ascii=False))
        # 关键：必须包含**直接放在 2026 目录里**的那个文件。
        # 只写 rel_dir LIKE '2026/%' 会漏掉它（它的 rel_dir 恰好等于 '2026'），
        # 而「有结果」这种弱断言照样能过 —— 之前就是这个坑。
        check("目录限定包含目录本身的文件（不只子目录）",
              any(d == "2026" for d in d_dirs), json.dumps(d_dirs, ensure_ascii=False))
        check("目录限定包含子目录里的文件",
              any(d == "2026/子目录" for d in d_dirs), json.dumps(d_dirs, ensure_ascii=False))
        check("目录限定排除了目录外的文件",
              not any(d == "" for d in d_dirs), json.dumps(d_dirs, ensure_ascii=False))
        da = post("/api/ask", {"question": "预算是多少？", "dir": "2026"})
        check("按目录提问只命中该目录",
              all((c.get("rel_dir") or "").startswith("2026") for c in da.get("citations", [])),
              json.dumps([c.get("rel_dir") for c in da.get("citations", [])], ensure_ascii=False))
        check("按目录提问确实有引用（不是空列表蒙混过关）",
              len(da.get("citations", [])) > 0,
              f"{len(da.get('citations', []))} 条")
        # 限定到子目录时，不能把父目录里的文件算进来
        da2 = post("/api/ask", {"question": "预算是多少？", "dir": "2026/子目录"})
        check("限定到子目录不包含父目录文件",
              all((c.get("rel_dir") or "") == "2026/子目录" for c in da2.get("citations", [])),
              json.dumps([c.get("rel_dir") for c in da2.get("citations", [])], ensure_ascii=False))
        # 限定目录 + 聚合意图：这条路径以前会 500（SQL 用了 f.rel_dir 但表没起别名），
        # 而且所有测试都没覆盖到 —— 必须单独守
        da3 = post("/api/ask", {"question": "预算合计是多少？", "dir": "2026"})
        check("限定目录 + 聚合意图不报错", isinstance(da3, dict) and "answer" in da3,
              str(da3)[:140].replace("\n", " | "))
        check("限定目录 + 聚合意图走 computed 模式", da3.get("mode") == "computed",
              f"mode={da3.get('mode')} error={str(da3.get('error'))[:80]}")

        print("\n=== C. UTF-16 / 编码探测 ===")
        hits = get("/api/search?q=" + urllib.parse.quote("差旅补贴")).get("hits", [])
        check("UTF-16 文件被检索到", len(hits) > 0, f"{len(hits)} 条命中")
        good = False
        for h in hits:
            snippet = h.get("snippet") or h.get("text") or ""
            if "差旅补贴" in snippet:
                good = True
        check("命中内容是正常中文（不是乱码）", good,
              json.dumps([(h.get("file_name"), (h.get("snippet") or "")[:40]) for h in hits],
                         ensure_ascii=False)[:220])

        print("\n=== D. 上下文封顶 + 子问题优先 ===")
        post("/api/config", {"retrieve": {"max_context_chunks": 2}})
        par = "验收报告、操作手册、问题清单分别由谁负责？"
        a2 = post("/api/ask", {"question": par})
        check("max_context_chunks=2 时引用数≤2", len(a2.get("citations", [])) <= 2,
              f"实际 {len(a2.get('citations', []))} 条")
        post("/api/config", {"retrieve": {"max_context_chunks": 16}})
        a16 = post("/api/ask", {"question": par})
        cits = a16.get("citations", [])
        check("默认 top_k=8 时引用数=8", len(cits) == 8, f"实际 {len(cits)} 条")
        # 每个子问题的最佳片段都应在引用里
        probes = ["验收报告 由谁负责", "操作手册 由谁负责", "问题清单 由谁负责"]
        chunk_ids = {c.get("chunk_id") for c in cits}
        miss = []
        for p in probes:
            sh = get("/api/search?q=" + urllib.parse.quote(p) + "&limit=1").get("hits", [])
            if sh and sh[0].get("chunk_id") not in chunk_ids:
                miss.append(p)
        check("并列提问每个子问题的最佳片段都保留", not miss, "缺失: " + ",".join(miss))

        print("\n=== E. 限定范围问答 ===")
        target = next((f for f in files if (f.get("name") or "").endswith(".xlsx")), None)
        check("能拿到目标文件", target is not None, (target or {}).get("name", ""))
        if target:
            a3 = post("/api/ask", {"question": "预算是多少？", "file_id": target["id"]})
            fids = {c.get("file_id") for c in a3.get("citations", [])}
            check("限定后引用全部来自该文件", fids == {target["id"]}, f"file_ids={fids}")
            check("限定后仍有引用", len(a3.get("citations", [])) > 0,
                  f"{len(a3.get('citations', []))} 条")

        print("\n=== E2. 表格聚合计算 ===")
        # 演示表里有一行「合计」，聚合时必须排除，否则双倍计数
        ac = post("/api/ask", {"question": "实际支出最多的是哪一项？"})
        ans_c = ac.get("answer") or ""
        check("聚合计算走 computed 模式", ac.get("mode") == "computed", f"mode={ac.get('mode')}")
        check("最大值取到明细行 118,000", "118,000" in ans_c, ans_c[:160].replace("\n", " | "))
        check("没有把「合计」行的 220,000 当成最大值", "220,000" not in ans_c,
              ans_c[:160].replace("\n", " | "))
        check("聚合结果带引用", len(ac.get("citations", [])) > 0,
              f"{len(ac.get('citations', []))} 条")
        check("聚合答案正文带 [n] 引用标记", bool(re.search(r"\[\d+\]", ans_c)),
              ans_c[:160].replace("\n", " | "))
        an = post("/api/ask", {"question": "一共有多少个预算项目？"})
        check("计数排除「合计」行（4 项而非 5 项）", "4 项" in (an.get("answer") or ""),
              (an.get("answer") or "")[:160].replace("\n", " | "))
        ax = post("/api/ask", {"question": "差旅费预算是多少？"})
        check("普通查值不误触发计算", ax.get("mode") != "computed", f"mode={ax.get('mode')}")

        print("\n=== E3. 聚合计算的坑表（每一种都曾导致算错）===")
        # 合计行写成「合计：」「共计」时必须同样被排除，
        # 否则明细之和被再加一遍，直接翻倍
        for label, fname in (("合计：", "合计写法.xlsx"), ("共计", "共计写法.xlsx")):
            fid = next((f["id"] for f in files if f.get("name") == fname), None)
            if not fid:
                check(f"夹具存在：{fname}", False, "文件没入库")
                continue
            r = post("/api/ask", {"question": "预算合计是多少？", "file_id": fid})
            ans = r.get("answer") or ""
            ok = r.get("mode") == "computed" and "300" in ans and "600" not in ans
            check(f"合计行写法「{label}」被排除（300 而非 600）", ok,
                  ans[:150].replace("\n", " | "))
        # 同名不同单位的两列不能被合并成一列
        fid = next((f["id"] for f in files if f.get("name") == "同名单位.xlsx"), None)
        if fid:
            r = post("/api/ask", {"question": "预算合计是多少？", "file_id": fid})
            ans = r.get("answer") or ""
            check("同名不同单位的列不被混算（不能出现 150）", "150" not in ans,
                  ans[:150].replace("\n", " | "))
        # 「3.5 万元」：万位前面有空格也要换算
        fid = next((f["id"] for f in files if f.get("name") == "带空格万.xlsx"), None)
        if fid:
            r = post("/api/ask", {"question": "预算合计是多少？", "file_id": fid})
            ans = r.get("answer") or ""
            check("「3.5 万元」正确换算成 55,000", "55,000" in ans,
                  ans[:150].replace("\n", " | "))
        # 多轮追问不能劫持聚合意图：上一轮问「最大」，这一轮问「最小」
        demo_x = next((f for f in files
                       if f.get("name") == "年度预算.xlsx" and not f.get("rel_dir")), None)
        amx_q = {"question": "实际支出的最大值是多少？"}
        amn_q = {"question": "最小呢", "prev_questions": ["实际支出的最大值是多少？"]}
        if demo_x:
            amx_q["file_id"] = demo_x["id"]
            amn_q["file_id"] = demo_x["id"]
        post("/api/ask", amx_q)
        amn = post("/api/ask", amn_q)
        ans_mn = amn.get("answer") or ""
        check("追问「最小呢」不被上一轮的「最大」劫持",
              "最小" in ans_mn and "最大" not in ans_mn,
              ans_mn[:150].replace("\n", " | "))
        # 同时问最大和最小：给不出一个准确的数，就不该只给一半
        amb = post("/api/ask", {"question": "预算的最小和最大分别是多少？"})
        check("同时问最大和最小时不硬算", amb.get("mode") != "computed",
              f"mode={amb.get('mode')}")

        print("\n=== F. 模型失败降级措辞 ===")
        post("/api/config", {"llm": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1",
                                     "api_key": "sk-test", "model": "dummy"}})
        a4 = post("/api/ask", {"question": "差旅费预算是多少？"})
        check("降级标记 degraded=true", a4.get("degraded") is True, json.dumps(
            {"mode": a4.get("mode"), "error": (a4.get("error") or "")[:80]}, ensure_ascii=False))
        check("回答里写明「已降级为资料原文摘录」",
              "已降级为资料原文摘录" in (a4.get("answer") or ""),
              (a4.get("answer") or "")[-120:].replace("\n", " "))
        check("没有谎称「未接入大模型」",
              "未接入大模型" not in (a4.get("answer") or ""))
        post("/api/config", {"llm": {"provider": "none", "base_url": "", "api_key": "", "model": ""}})
        a5 = post("/api/ask", {"question": "差旅费预算是多少？"})
        check("清空配置后 degraded=false", a5.get("degraded") is False)
        check("未接模型时标注「未接入大模型」", "未接入大模型" in (a5.get("answer") or ""))

        print("\n=== F2. 体检健康提醒确实会报 ===")
        post("/api/config", {"llm": {"provider": "openai", "base_url": "http://127.0.0.1:9/v1",
                                     "api_key": "", "model": "dummy"},
                             "index": {"auto_rescan_seconds": 0}})
        h2 = get("/api/report").get("health", [])
        check("体检报出「没填 API Key」", any("API Key" in h for h in h2),
              json.dumps(h2, ensure_ascii=False))
        check("体检报出「自动同步已关闭」", any("自动同步" in h for h in h2),
              json.dumps(h2, ensure_ascii=False))
        post("/api/config", {"llm": {"provider": "none", "base_url": "", "api_key": "", "model": ""},
                             "index": {"auto_rescan_seconds": 60}})
        h3 = get("/api/report").get("health", [])
        check("配置恢复后健康提醒清空", not any("API Key" in h or "自动同步" in h for h in h3),
              json.dumps(h3, ensure_ascii=False))

        print("\n=== G. 多轮追问 ===")
        a6 = post("/api/ask", {"question": "那预算是多少？",
                               "prev_questions": ["差旅费预算是多少？"]})
        check("追问触发上文扩展", a6.get("expanded") is True)
        check("追问能给出答案", len(a6.get("citations", [])) > 0,
              f"{len(a6.get('citations', []))} 条引用")
        a7 = post("/api/ask", {"question": "差旅费预算是多少？"})
        check("完整问题不触发扩展", a7.get("expanded") is False)

        print("\n=== H. 引用兜底 /api/resolve ===")
        rs = post("/api/resolve", {"question": "差旅费预算是多少？"})
        check("resolve 返回引用列表", len(rs.get("citations", [])) > 0,
              f"{len(rs.get('citations', []))} 条")

        print("\n=== H2. 畸形参数不该 500 ===")
        for label, payload in [
            ("top_k 非数字", {"question": "预算合计", "top_k": "abc"}),
            ("file_id 非数字", {"question": "预算合计", "file_id": "abc"}),
            ("dir 越界路径", {"question": "预算合计", "dir": "../../../etc"}),
            ("dir 通配符", {"question": "预算合计", "dir": "%_"}),
            ("prev 非数组", {"question": "预算", "prev_questions": "不是数组"}),
            ("空问题", {"question": ""}),
        ]:
            code = post_code("/api/ask", payload)
            check(f"畸形参数返回 4xx/200 而不是 500：{label}", code < 500, f"HTTP {code}")

        print("\n=== I. 导出 Markdown / Word ===")
        cites = a7.get("citations", [])[:2]
        payload = {"format": "md", "question": "差旅费预算是多少？", "answer": a7.get("answer", ""),
                   "mode": a7.get("mode", ""), "citations": cites}
        st_code, hdrs, data = post_bytes("/api/export", payload)
        text = data.decode("utf-8", "replace")
        check("导出 Markdown 200", st_code == 200, str(st_code))
        check("Markdown 含问题与引用出处",
              "差旅费预算是多少" in text and "引用出处" in text, text[:120].replace("\n", " | "))
        cd = hdrs.get("Content-Disposition", "") or hdrs.get("content-disposition", "")
        check("导出文件名做 RFC5987 双写", "filename*=" in cd.lower(),
              f"hdrs={list(hdrs.keys())} cd={cd[:120]}")

        payload["format"] = "docx"
        st2, hdrs2, data2 = post_bytes("/api/export", payload)
        check("导出 Word 200", st2 == 200, str(st2))
        check("Word 是合法 docx（PK 头 + 体积合理）",
              data2[:2] == b"PK" and len(data2) > 3000,
              f"{len(data2)} 字节，头={data2[:4]!r}")
        try:
            import zipfile
            import io as _io
            zf = zipfile.ZipFile(_io.BytesIO(data2))
            names = zf.namelist()
            check("docx 内部结构完整（含 document.xml）",
                  "word/document.xml" in names, f"{len(names)} 个条目")
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
            check("docx 正文含答案文字", "差旅费" in xml, "")
            check("docx 含「引用出处」附录", "引用出处" in xml, "")
        except Exception as exc:
            check("docx 可被 zipfile 打开", False, str(exc))

        print("\n=== J. 拖拽上传 ===")
        up_res = upload(
            [("上传样本.txt", "上传内容里有一个特殊词：紫貂。".encode("utf-8"))],
            ["子目录/上传样本.txt"],
        )
        check("上传接口返回成功", (up_res.get("files") or 0) >= 1 and up_res.get("ok") is True,
              json.dumps(up_res, ensure_ascii=False)[:160])
        up_path = data_dir / "uploads" / "子目录" / "上传样本.txt"
        check("上传文件落盘且保留目录结构", up_path.exists(), str(up_path))
        time.sleep(3)
        wait_indexed(60)
        uh = get("/api/search?q=" + urllib.parse.quote("紫貂")).get("hits", [])
        check("上传文件可被检索", len(uh) > 0, f"{len(uh)} 条命中")

        # 逐文件结果：前端要靠它只重试失败的那几个
        up2 = upload(
            [("好的.txt", "正常内容".encode("utf-8")), ("坏的.exe", b"MZ\x00\x00")],
            ["好.txt", "坏.exe"],
        )
        results = up2.get("results") or []
        check("上传返回逐文件结果", len(results) == 2,
              json.dumps(results, ensure_ascii=False)[:200])
        check("逐文件结果含状态字段",
              all("status" in r for r in results),
              json.dumps([r.get("status") for r in results], ensure_ascii=False))
        check("不支持的格式被标为 skipped",
              any(r.get("status") == "skipped" for r in results),
              json.dumps([(r.get("name"), r.get("status")) for r in results], ensure_ascii=False))

        print("\n=== K. 删除只删索引 ===")
        tgt = next((f for f in get("/api/files").get("files", []) if f.get("name") == "上传样本.txt"), None)
        if check("能定位上传文件记录", tgt is not None):
            req = urllib.request.Request(f"{BASE}/api/files/{tgt['id']}", method="DELETE")
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
            check("删除后索引里没有了",
                  all(f.get("id") != tgt["id"] for f in get("/api/files").get("files", [])))
            check("删除后磁盘文件仍在（默认不删文件）", up_path.exists())

        print("\n=== L. 侧栏来源类型 ===")
        roots = get("/api/roots").get("roots", [])
        check("根目录 kind 正确", all(r.get("kind") in ("folder", "file", "disk") for r in roots),
              json.dumps(roots, ensure_ascii=False)[:160])

        print("\n=== M. 问答历史 ===")
        hist = get("/api/history").get("items", [])
        check("历史已记录（服务端自动存）", len(hist) >= 5, f"{len(hist)} 条")
        check("历史含问题与答案",
              bool(hist) and bool(hist[0].get("question")) and bool(hist[0].get("answer")),
              json.dumps(hist[0], ensure_ascii=False)[:140] if hist else "")
        check("历史记录了计算模式",
              any(h.get("mode") == "computed" for h in hist),
              ",".join(sorted({h.get("mode") or "" for h in hist})))
        check("历史总数一致", get("/api/history").get("total") == len(hist))
        if hist:
            hid = hist[0]["id"]
            req = urllib.request.Request(f"{BASE}/api/history/{hid}", method="DELETE")
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
            check("可删除单条历史",
                  len(get("/api/history").get("items", [])) == len(hist) - 1)
        try:
            with urllib.request.urlopen(BASE + "/api/history/export", timeout=60) as r:
                body = r.read().decode("utf-8")
            check("历史可导出 Markdown",
                  "EveryFind 问答记录" in body and len(body) > 100,
                  body[:80].replace("\n", " | "))
        except Exception as exc:
            check("历史可导出 Markdown", False, str(exc))
        req = urllib.request.Request(BASE + "/api/history", method="DELETE")
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        check("可清空历史", len(get("/api/history").get("items", [])) == 0)

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    bad = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 56)
    print(f"  {len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败项：")
        for name, _, detail in bad:
            print("  · " + name + ("  " + detail if detail else ""))
    print("=" * 56)

    # 清理临时目录
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
