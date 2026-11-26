"""
Shared asynchronous Gemini REST client for Trillionaire AI modules.
Uses httpx directly so both text coaching (Trade Autopsy) and multimodal PNG chart
inspection (Vision Trap Filter) execute with strict timeouts and model fallback.
"""

import json
import re
from typing import Any, Dict, Iterable, List, Optional
import httpx
from loguru import logger

from config.settings import settings

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _strip_json_fences(raw: str) -> str:
    cleaned = raw.strip()
    fence_match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", cleaned, flags=re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return cleaned


async def call_gemini_async(
    parts: List[Dict[str, Any]],
    model_candidates: Iterable[str] = ("gemini-3.8-flash", "gemini-2.5-pro"),
    timeout_sec: float = 12.0,
    api_key: Optional[str] = None,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """
    Calls Google Gemini generateContent REST API across fallback models with JSON output mode.
    Returns {'ok': True, 'model': ..., 'data': ..., 'raw_text': ...} or {'ok': False, 'error': ...}.
    """
    key = (api_key if api_key is not None else settings.gemini_api_key or "").strip()
    if not key or key.startswith("your_"):
        return {"ok": False, "error": "GEMINI_API_KEY not configured"}

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }

    last_error = "No models attempted"
    async with httpx.AsyncClient(timeout=timeout_sec) as client:
        for model in model_candidates:
            url = f"{GEMINI_BASE_URL}/{model}:generateContent?key={key}"
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    last_error = f"{model} HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.warning(f"[GEMINI CLIENT] {last_error}")
                    continue

                body = resp.json()
                candidates = body.get("candidates") or []
                if not candidates:
                    last_error = f"{model} returned empty candidates"
                    continue

                part_list = candidates[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in part_list if isinstance(p, dict)).strip()
                if not text:
                    last_error = f"{model} returned empty text"
                    continue

                cleaned = _strip_json_fences(text)
                parsed = json.loads(cleaned)
                return {
                    "ok": True,
                    "model": model,
                    "data": parsed,
                    "raw_text": text,
                }
            except Exception as exc:
                last_error = f"{model} error: {exc}"
                logger.warning(f"[GEMINI CLIENT] {last_error}")

    return {"ok": False, "error": last_error}
