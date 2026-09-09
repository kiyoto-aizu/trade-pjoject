import json
import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)


class OpenAIDailyAnalyzer:
    """日次取引サマリーをLLMで評価するアダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def analyze(self, daily_summary: dict) -> str | None:
        prompt = (
            "以下は自動売買の1日分の機械集計です。数値を再計算せず、与えられた数値だけを根拠に、"
            "日本語で短く評価してください。出力は「今日の評価」「観察できる仮説」「今後の方針」の3見出し、"
            "各見出し2項目以内とし、方針はロジック変更を命令せず、追加で観測すべき点として書いてください。"
            "投資判断や売買指示は行わず、これは検証期間中の参考意見だと明記してください。\n\n"
            f"日次集計:\n{json.dumps(daily_summary, ensure_ascii=False, indent=2)}"
        )
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
                        {"role": "system", "content": "あなたは自動売買システムの日次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:1800]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("日次LLM分析に失敗しました: %s", exc)
            return None


def create_daily_analyzer() -> OpenAIDailyAnalyzer | None:
    if not config.LLM_DAILY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIDailyAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )