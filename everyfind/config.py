"""配置与路径管理。

配置持久化在 <data>/config.json，索引库在 <data>/index.db。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_config_lock = threading.RLock()

APP_NAME = "EveryFind"
APP_TITLE = "EveryFind · 本地文档内容检索"

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        # none | openai | ollama
        "provider": "none",
        "base_url": "",
        "api_key": "",
        "model": "",
        "temperature": 0.1,
        "max_tokens": 1200,
        "timeout": 60,
    },
    "ocr": {
        "enabled": True,
        "min_confidence": 0.45,
        "ocr_scanned_pdf": True,
    },
    "index": {
        "max_file_mb": 120,
        # 自动同步：往已添加的文件夹里丢新文件/改文件，多久自动入库（秒）。0 = 关闭，只能手动点「刷新」
        "auto_rescan_seconds": 60,
        # 整盘扫描代价高得多，默认 30 分钟一次
        "auto_rescan_disk_seconds": 1800,
    },
    "retrieve": {
        "top_k": 8,
        "candidates": 60,
        # 送进模型的片段数上限。top_k 是软目标，但并列提问每个子问题
        # 至少要各留一个片段，所以实际上限取这两者的较大值再封顶。
        "max_context_chunks": 16,
    },
    "ui": {
        "open_browser": True,
    },
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录（源码模式=项目根，冻结模式=exe 所在目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读资源目录（web/ 前端资源）。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / "web"
        return Path(sys.executable).resolve().parent / "web"
    return app_dir() / "web"


def data_dir() -> Path:
    env = os.environ.get("EVERYFIND_DATA")
    p = Path(env).expanduser() if env else app_dir() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "index.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    if not isinstance(override, dict):
        raise ValueError("配置必须是 JSON 对象")
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = config_path()
    if p.exists():
        try:
            cfg = _deep_merge(cfg, json.loads(p.read_text("utf-8")))
            validate_config(cfg)
        except Exception as exc:
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            # 配置坏了不能悄悄回落默认值——那等于把用户的 API Key 弄丢，
            # 而界面上看不出任何异常。把坏文件改名留档，并提示用户。
            broken = p.with_name(p.stem + ".损坏-" + str(time.time_ns()) + p.suffix)
            try:
                p.replace(broken)
            except OSError:
                broken = None
            sys.stderr.write(
                f"[EveryFind] config.json 解析失败（{exc}），已回退默认配置。"
                + (f"原文件已保留为 {broken.name}" if broken else "")
                + "\n"
            )
    secret = secrets_path()
    if secret.exists():
        # 凭据文件损坏时明确失败，不能悄悄覆盖掉已有密钥。
        cfg['llm']['api_key'] = json.loads(secret.read_text('utf-8')).get('api_key', '')
    return cfg


def secrets_path() -> Path:
    return data_dir().parent / 'local-secrets' / 'api-keys' / 'llm.json'


def validate_config(cfg: dict) -> None:
    for section in DEFAULT_CONFIG:
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f'{section} 必须是对象')
    for field in ('provider', 'base_url', 'api_key', 'model'):
        if not isinstance(cfg['llm'].get(field, ''), str):
            raise ValueError(f'llm.{field} 必须是文本')
    if cfg['llm']['provider'] not in ('none', 'openai', 'ollama'):
        raise ValueError('provider 必须是 none / openai / ollama')
    for sec, key, low, high in (
        ('llm', 'temperature', 0, 2), ('llm', 'timeout', 1, 600),
        ('llm', 'max_tokens', 1, 1000000), ('ocr', 'min_confidence', 0, 1),
        ('index', 'max_file_mb', 0.001, 10240),
        ('index', 'auto_rescan_seconds', 0, 86400),
        ('index', 'auto_rescan_disk_seconds', 0, 604800),
        ('retrieve', 'top_k', 1, 100), ('retrieve', 'candidates', 1, 1000),
        ('retrieve', 'max_context_chunks', 1, 100),
    ):
        value = cfg[sec].get(key, DEFAULT_CONFIG[sec][key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ValueError(f'{sec}.{key} 必须在 {low} 至 {high} 之间')
    for sec, key in [('ocr', 'enabled'), ('ocr', 'ocr_scanned_pdf'), ('ui', 'open_browser')]:
        if not isinstance(cfg[sec].get(key), bool):
            raise ValueError(f'{sec}.{key} 必须是布尔值')


def _atomic_json(path: Path, value: dict, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if private:
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    with _config_lock:
        merged = _deep_merge(DEFAULT_CONFIG, cfg or {})
        validate_config(merged)
        public = json.loads(json.dumps(merged))
        api_key = public['llm'].pop('api_key', '')
        _atomic_json(secrets_path(), {'api_key': api_key}, private=True)
        _atomic_json(config_path(), public)
        return merged
