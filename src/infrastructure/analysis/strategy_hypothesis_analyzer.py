from __future__ import annotations

import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """あなたは日本株自動売買システムの「戦略仮説アドバイザー」です。
役割は仮説の提示のみで、パラメータ変更の決定はしません。
以下のルールを厳守してください:
1. 観察は与えられたデータの事実のみを述べる。推測を混ぜない
2. 仮説は必ず「この観察が根拠」と紐付ける。根拠のない仮説は出さない
3. 一つの仮説につき、それを否定しうる反証データも必ず挙げる
4. 既に検証済み・却下済みの仮説(過去ADR参照)は繰り返さない
5. 「閾値をX円にすべき」のような具体的な数値変更の提案はしない。
   代わりに「何を追加で調べれば判断できるか」を提案する"""


class OpenAIStrategyHypothesisAnalyzer:
    """既存LLM設定とChat Completions HTTP方式を使う戦略仮説アダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = config.LLM_TIMEOUT_SECONDS):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def analyze(self, user_prompt: str) -> str | None:
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:8000]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("戦略仮説分析に失敗しました: %s", exc)
            return None


def create_strategy_hypothesis_analyzer() -> OpenAIStrategyHypothesisAnalyzer | None:
    if not config.LLM_DAILY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIStrategyHypothesisAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )