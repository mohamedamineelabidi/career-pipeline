"""Shared LLM client for Career Pipeline (Groq, OpenAI-compatible).

Rules: reads GROQ_API_KEY from env or ./.env; never logs the key; every call is
optional (callers must degrade gracefully when llm_available() is False); the
model only REORGANISES and SCORES from supplied evidence, never invents facts.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = "openai/gpt-oss-120b"
BASE_URL = "https://api.groq.com/openai/v1"


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()


def api_key() -> str | None:
    return os.environ.get("GROQ_API_KEY") or None


def model_name() -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODEL


def llm_available() -> bool:
    return bool(api_key()) and os.environ.get("LLM_DISABLED", "") != "1"


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict[str, str]], *, json_mode: bool = False, max_tokens: int = 1200,
         temperature: float = 0.1, retries: int = 2, timeout: int = 60) -> str:
    """Return assistant text. Raises LLMError when unavailable or failing."""
    key = api_key()
    if not key:
        raise LLMError("GROQ_API_KEY not configured")
    body: dict[str, Any] = {
        "model": model_name(),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if "gpt-oss" in body["model"]:
        body["reasoning_effort"] = "low"
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            f"{BASE_URL}/chat/completions", data=data, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "User-Agent": "career-pipeline/1.0 (+local)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choice = payload["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                raise LLMError("empty completion (finish_reason=%s)" % choice.get("finish_reason"))
            return content
        except urllib.error.HTTPError as error:
            last = error
            if error.code == 429 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            detail = error.read().decode("utf-8", "replace")[:300]
            raise LLMError(f"HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, LLMError) as error:
            last = error
            if attempt < retries:
                time.sleep(1.5)
                continue
    raise LLMError(str(last))


def chat_json(messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
    messages = [{"role": "system", "content": "Respond with a single valid JSON object only."}] + list(messages)
    text = chat(messages, json_mode=True, **kwargs)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LLMError("no JSON object in completion")
    return json.loads(text[start:end + 1])


if __name__ == "__main__":
    print("available:", llm_available(), "model:", model_name())
    if llm_available():
        print(chat_json([{"role": "user", "content": 'Return exactly {"ok": true}'}], max_tokens=200))
