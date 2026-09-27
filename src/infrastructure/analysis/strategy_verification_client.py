from __future__ import annotations

import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)


class OpenAIStrategyVerificationClient:
    """既存のOpenAI Chat Completions設定を使う仮説検証クライアント。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = 90.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("OpenAI応答に本文がありません")
        return content.strip()


def create_strategy_verification_client() -> OpenAIStrategyVerificationClient | None:
    if not config.LLM_DAILY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIStrategyVerificationClient(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )