"""OpenAI 相容 Chat Completions 客戶端（僅標準庫）。

只要服務支援 /chat/completions 與 JSON 輸出，就能直接使用：
  - OpenAI
  - Azure OpenAI（請把 base_url 指向對應 deployment）
  - DeepSeek / Moonshot / 阿里雲百鍊 / 智譜 / 本地 vLLM、Ollama 等
環境變數：TEM_LLM_API_KEY / TEM_LLM_BASE_URL / TEM_LLM_MODEL / TEM_LLM_ENABLED
"""

from __future__ import annotations

import json
import re
from typing import Any

from .http import post_json

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 90) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        json_mode: bool = True,
        thinking: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if thinking:
            payload["thinking"] = {"type": thinking}
        data = post_json(
            f"{self.base_url}/chat/completions",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"回應格式非預期：{json.dumps(data)[:300]}") from exc

    def complete_json(self, system: str, user: str, **kwargs: Any) -> Any:
        raw = self.complete(system, user, **kwargs)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            match = _JSON_BLOCK.search(raw)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            raise LLMError(f"無法解析 JSON：{raw[:300]}")
