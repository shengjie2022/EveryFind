"""LLM 接入层：OpenAI 兼容接口 / Ollama / 无模型降级。

只有"提问 + 检索到的片段"会发给模型，原始文档不会离开本机（使用云端模型时亦如此）。
"""
from __future__ import annotations

import json
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def _cfg_llm(cfg: dict) -> dict:
    return (cfg or {}).get("llm") or {}


def provider_of(cfg: dict) -> str:
    return (_cfg_llm(cfg).get("provider") or "none").strip().lower()


def is_configured(cfg: dict) -> bool:
    p = provider_of(cfg)
    llm = _cfg_llm(cfg)
    if p == "none":
        return False
    if p == "ollama":
        return bool(llm.get("base_url") or llm.get("model"))
    return bool(llm.get("base_url") and llm.get("model"))


def describe(cfg: dict) -> dict:
    llm = _cfg_llm(cfg)
    p = provider_of(cfg)
    return {
        "provider": p,
        "model": llm.get("model") or "",
        "base_url": llm.get("base_url") or "",
        "has_api_key": bool(llm.get("api_key")),
        "configured": is_configured(cfg),
    }


def chat(messages: list[dict], cfg: dict, timeout: int | None = None) -> tuple[str | None, str]:
    """返回 (回答文本, 错误信息)。任一失败都返回错误，调用方负责降级。"""
    if requests is None:
        return None, "缺少 requests 依赖"
    p = provider_of(cfg)
    llm = _cfg_llm(cfg)
    model = llm.get("model") or ""
    base = (llm.get("base_url") or "").rstrip("/")
    tmo = int(timeout or llm.get("timeout") or 60)

    try:
        if p == "openai":
            if not base or not model:
                return None, "未配置 base_url / model"
            url = base if base.endswith("/chat/completions") else base + "/chat/completions"
            headers = {"Content-Type": "application/json"}
            if llm.get("api_key"):
                headers["Authorization"] = f"Bearer {llm['api_key']}"
            payload = {
                "model": model,
                "messages": messages,
                "temperature": float(llm.get("temperature", 0.1)),
                "max_tokens": int(llm.get("max_tokens", 1200)),
            }
            r = requests.post(url, json=payload, headers=headers, timeout=tmo)
            if r.status_code >= 400:
                return None, f"模型接口返回 {r.status_code}: {r.text[:300]}"
            data = r.json()
            text = _extract_openai(data)
            # HTTP 200 但正文是空的，也算失败。返回空错误会让上层以为
            # 「压根没接模型」，把实际调用过却没出结果说成没配置 —— 那是谎报。
            if not text:
                return None, "模型返回了空内容"
            return text, ""

        if p == "ollama":
            url = (base or "http://127.0.0.1:11434").rstrip("/")
            if url.endswith("/api/chat"):
                url = url[: -len("/api/chat")]
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": float(llm.get("temperature", 0.1)),
                    "num_predict": int(llm.get("max_tokens", 1200)),
                },
            }
            r = requests.post(url + "/api/chat", json=payload, timeout=tmo)
            if r.status_code >= 400:
                return None, f"Ollama 返回 {r.status_code}: {r.text[:300]}"
            data = r.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
            if not text:
                return None, "模型返回了空内容"
            return text, ""

        return None, "未配置模型（provider=none）"
    except Exception as exc:
        return None, f"调用模型失败: {exc}"


def _extract_openai(data: dict) -> str:
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


def probe(cfg: dict) -> dict:
    """连通性自检。"""
    if not is_configured(cfg):
        return {"ok": False, "message": "未配置模型"}
    text, err = chat([{"role": "user", "content": "回复 OK"}], cfg, timeout=20)
    if err:
        return {"ok": False, "message": err}
    return {"ok": True, "message": f"连接正常：{text[:40]}"}


def test_ollama_models(base_url: str = "http://127.0.0.1:11434") -> list[str]:
    """列出本机 Ollama 已拉取的模型。"""
    if requests is None:
        return []
    try:
        r = requests.get(base_url.rstrip("/") + "/api/tags", timeout=5)
        if r.status_code >= 400:
            return []
        data = r.json()
        return [m.get("name") for m in (data.get("models") or []) if m.get("name")]
    except Exception:
        return []
