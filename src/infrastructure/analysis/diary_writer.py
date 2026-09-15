import json
import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)


class NoteDiaryWriter:
    """日次の検証材料からnote.com向け日記本文を生成するアダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def write(self, context: dict) -> str | None:
        prompt = (
            "これは個人の検証プロジェクトの日記です。以下の材料をもとに、note.comに投稿する親しみやすい"
            "日記記事を日本語のですます体で書いてください。材料はJSONです。"
            "「今日やったこと」「結果」「気づき・つまずいた点」「次にやりたいこと」を目安にMarkdown見出しを"
            "使ってください。ただし材料が薄い項目は無理に書かず、省略して構いません。"
            "manual_notesに内容がある場合は最優先の一次情報として扱い、git commitやトレード結果は補足として"
            "使ってください。数値の再計算はしないでください。投資助言や売買指示は書かず、検証期間中の個人メモ"
            "である旨を明記してください。600〜1000字程度にしてください。\n\n"
            f"材料:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
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
                        {"role": "system", "content": "あなたは個人開発の日記編集者です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("日記LLM生成に失敗しました: %s", exc)
            return None


def create_diary_writer() -> NoteDiaryWriter | None:
    if not config.LLM_DIARY_ENABLED or not config.LLM_API_KEY:
        return None
    return NoteDiaryWriter(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )