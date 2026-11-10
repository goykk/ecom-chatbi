"""LLM 客户端：OpenAI 兼容接口 + Ollama 原生接口。

为什么要有两条通道：
- 线上用 DeepSeek / Qwen 的 OpenAI 兼容接口；
- 本地没有 API key 时用 Ollama。但 Ollama 的 /v1 兼容层对「思考型模型」支持不完整
  （实测 qwen3.5 / gemma4 会把 token 全耗在 reasoning 上，content 为空、finish_reason=length），
  必须走原生 /api/chat 并显式 think=false 才能拿到最终 JSON。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Sequence

import httpx


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    provider: str = "openai"          # openai / ollama
    timeout: float = 60.0
    max_retries: int = 2
    think: bool = False               # 仅 ollama 生效：False 时关闭思考链

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            base_url=os.getenv("LLM_BASE_URL", cls.base_url).rstrip("/"),
            api_key=os.getenv("LLM_API_KEY", "ollama"),
            model=os.getenv("LLM_MODEL", cls.model),
            provider=os.getenv("LLM_PROVIDER", "openai").lower(),
            timeout=float(os.getenv("LLM_TIMEOUT", "60")),
            think=os.getenv("LLM_THINK", "0") == "1",
        )


class LLMClient:
    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()

    def chat(self, messages: Sequence[dict], temperature: float = 0.0,
             json_mode: bool = False) -> str:
        if self.config.provider == "ollama":
            return self._chat_ollama(messages, temperature, json_mode)
        return self._chat_openai(messages, temperature, json_mode)

    # ---------------- OpenAI 兼容 ----------------
    def _chat_openai(self, messages: Sequence[dict], temperature: float,
                     json_mode: bool) -> str:
        if not self.config.api_key:
            raise LLMError("缺少 LLM_API_KEY，无法调用模型")

        payload: dict = {"model": self.config.model, "messages": list(messages),
                         "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.config.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}

        last_error: Exception | None = None
        format_fallback = False
        for attempt in range(self.config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.config.timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code == 400 and json_mode and not format_fallback:
                    # 部分兼容服务不支持 response_format，去掉该字段重试一次
                    payload.pop("response_format", None)
                    json_mode = False
                    format_fallback = True
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                choice = data["choices"][0]
                content = (choice["message"].get("content") or "").strip()
                if not content:
                    raise LLMError(f"空返回（finish_reason={choice.get('finish_reason')}）")
                return content
            except (httpx.HTTPError, LLMError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                break
        raise LLMError(f"LLM 调用失败：{last_error}")

    # ---------------- Ollama 原生 ----------------
    def _chat_ollama(self, messages: Sequence[dict], temperature: float,
                     json_mode: bool) -> str:
        base = self.config.base_url
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/api/chat"
        payload: dict = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": False,
            "think": bool(self.config.think),
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"

        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with httpx.Client(timeout=self.config.timeout) as client:
                    resp = client.post(url, json=payload)
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                message = data.get("message") or {}
                content = (message.get("content") or "").strip()
                if not content:
                    # 思考型模型把 token 全花在 reasoning 上时会走到这里
                    raise LLMError(
                        f"空返回（done_reason={data.get('done_reason')}，"
                        f"thinking 长度={len(message.get('thinking') or '')}）；"
                        f"请确认 LLM_THINK=0 或换用非思考模型"
                    )
                return content
            except (httpx.HTTPError, LLMError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    time.sleep(0.6 * (2 ** attempt))
                    continue
                break
        raise LLMError(f"LLM 调用失败：{last_error}")
