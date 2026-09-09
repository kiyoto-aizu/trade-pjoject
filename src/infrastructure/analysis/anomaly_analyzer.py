"""
================================================================================
異常検知分析モジュール
スクリーニング/フィルタリング結果が普段と異なる可能性がある場合に、
LLMで一次分析コメントを生成するアダプターを提供します。
================================================================================
"""
import json
import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)


class OpenAIAnomalyAnalyzer:
    """スクリーニング/フィルタリング結果の異常値をLLMで一次分析するアダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def analyze_screening(self, summary: dict) -> str | None:
        return self._analyze(
            summary,
            system_role="あなたは自動売買システムのスクリーニング結果レビュー担当です。",
            instruction=(
                "以下は本日のスクリーニング集計です。与えられた数値だけを根拠に、"
                "採用銘柄数が普段より少ない・除外が偏っているなど異常の可能性がある点を、"
                "日本語で短く指摘してください。出力は「観測された異常」「考えられる要因」"
                "「確認すべきデータ」の3見出し、各見出し2項目以内としてください。"
                "断定はせず仮説として述べ、投資判断やロジック変更の指示は行わないでください。"
            ),
            data_label="スクリーニング集計",
        )

    def analyze_filtering(self, summary: dict) -> str | None:
        return self._analyze(
            summary,
            system_role="あなたは自動売買システムのフィルタリング結果レビュー担当です。",
            instruction=(
                "以下は本日のフィルタリング集計です。与えられた数値だけを根拠に、"
                "採用銘柄数が極端に少ない・出来高条件で大半が除外されているなど異常の"
                "可能性がある点を、日本語で短く指摘してください。出力は「観測された異常」"
                "「考えられる要因」「確認すべきデータ」の3見出し、各見出し2項目以内としてください。"
                "断定はせず仮説として述べ、投資判断やロジック変更の指示は行わないでください。"
            ),
            data_label="フィルタリング集計",
        )

    def _analyze(self, summary: dict, system_role: str, instruction: str, data_label: str) -> str | None:
        prompt = f"{instruction}\n\n{data_label}:\n{json.dumps(summary, ensure_ascii=False, indent=2)}"
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
                        {"role": "system", "content": system_role},
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
            logger.warning("異常検知のLLM分析に失敗しました: %s", exc)
            return None


def create_anomaly_analyzer() -> OpenAIAnomalyAnalyzer | None:
    if not config.LLM_ANOMALY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIAnomalyAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )
