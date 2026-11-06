"""EveryFind 启动器。

用法：
    python run.py                # 启动本地服务并打开浏览器
    python run.py --port 9000    # 指定端口
    python run.py --no-browser   # 不自动打开界面
    python run.py --window       # 用原生窗口打开（需要 pywebview）
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台中文输出
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from everyfind import config, db  # noqa: E402


def pick_port(preferred: int = 8765, host: str = "127.0.0.1") -> int:
    for p in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EveryFind · 本地文档内容检索")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--window", action="store_true", help="以原生窗口打开界面")
    ap.add_argument("--rebuild", action="store_true", help="启动时清空并重建索引")
    args = ap.parse_args()
    if not 1 <= args.port <= 65506:
        ap.error("端口必须在 1 至 65506 之间")

    db.init_db()
    if args.rebuild:
        db.reset_db()
        print("· 已清空索引库")

    port = pick_port(args.port, args.host)
    if not port:
        print("✗ 没有可用端口")
        return 1
    url = f"http://{args.host}:{port}"

    import uvicorn

    cfg = config.load_config()
    print("=" * 56)
    print("  EveryFind · 本地文档内容检索")
    print(f"  界面地址：{url}")
    print(f"  资料库　：{config.db_path()}")
    print(f"  模型　　：{cfg.get('llm', {}).get('model') or '未接入（原文摘录模式）'}")
    print("  提示    ：关闭此窗口即停止服务")
    print("=" * 56)

    if args.window:
        server = uvicorn.Server(uvicorn.Config('everyfind.server:app', host=args.host, port=port, log_level='warning'))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 30
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            worker.join(timeout=5)
            print('本地服务启动失败，请检查依赖和端口。')
            return 1
        try:
            import webview
            webview.create_window("EveryFind · 本地文档内容检索", url, width=1280, height=860)
            webview.start()
        except Exception:
            print('原生窗口不可用，已切换浏览器；关闭此终端可停止服务。')
            if not args.no_browser:
                webbrowser.open(url)
            try:
                worker.join()
            except KeyboardInterrupt:
                pass
        finally:
            server.should_exit = True
            worker.join(timeout=5)
        return 0

    if not args.no_browser:
        # ui.open_browser 是配置项，留个开关方便不弹浏览器的场景
        if (cfg.get("ui") or {}).get("open_browser", True):
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run("everyfind.server:app", host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
