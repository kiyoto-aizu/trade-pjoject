import hashlib
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

from src.config import config
from src.infrastructure.persistence.storage import read_json, write_json

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).resolve().parents[3] / "data" / "analysis" / "llm_error_state.json"
_STATE_RETENTION_DAYS = 14
_FRAME_DEPTH = 3

_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?")
_SYMBOL_PATTERN = re.compile(r"\b\d{4}[A-Z]?\b")
_NUMBER_PATTERN = re.compile(r"-?\d+\.\d+|-?\d+")


def _normalize_message(message: str) -> str:
    """銘柄コード・価格・日時など発生毎に変わる値をプレースホルダー化し、同一原因のメッセージを揃える。"""
    text = _TIMESTAMP_PATTERN.sub("<TIMESTAMP>", message)
    text = _SYMBOL_PATTERN.sub("<SYMBOL>", text)
    text = _NUMBER_PATTERN.sub("<NUM>", text)
    return text


@dataclass
class ErrorAnalysis:
    summary: str
    occurrence_count: int
    is_new: bool


class OpenAIErrorAnalyzer:
    """例外発生時の原因分析をLLMに依頼するアダプター。

    同一原因のエラーはフィンガープリントで束ね、初回のみLLMに問い合わせて結果をキャッシュし、
    以降はクールダウン期間中キャッシュを再利用（発生回数だけカウント）することでクレジット消費を抑える。
    """

    def __init__(self, api_key: str, model: str, api_url: str, cooldown_minutes: int, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.cooldown_minutes = cooldown_minutes
        self.timeout = timeout

    def analyze_exception(self, process_name: str, exc: BaseException) -> ErrorAnalysis | None:
        if self._is_minor_exception(exc):
            logger.info("軽微な例外(%s)のためLLM分析をスキップしました。", type(exc).__name__)
            return None

        fingerprint = self._fingerprint(exc)
        state = read_json(_STATE_FILE) or {}
        entry = state.get(fingerprint)
        now = datetime.now()

        if entry is not None:
            last_analyzed = self._parse_iso(entry.get("last_analyzed"))
            needs_refresh = last_analyzed is None or now - last_analyzed >= timedelta(minutes=self.cooldown_minutes)
            if needs_refresh:
                refreshed = self._call_llm(process_name, exc)
                if refreshed:
                    entry["analysis"] = refreshed
                    entry["last_analyzed"] = now.isoformat(timespec="seconds")
            entry["count"] = entry.get("count", 1) + 1
            entry["last_seen"] = now.isoformat(timespec="seconds")
            state[fingerprint] = entry
            self._persist(state)
            return ErrorAnalysis(summary=entry["analysis"], occurrence_count=entry["count"], is_new=False)

        analysis = self._call_llm(process_name, exc)
        if not analysis:
            return None
        state[fingerprint] = {
            "analysis": analysis,
            "count": 1,
            "first_seen": now.isoformat(timespec="seconds"),
            "last_seen": now.isoformat(timespec="seconds"),
            "last_analyzed": now.isoformat(timespec="seconds"),
        }
        self._persist(state)
        return ErrorAnalysis(summary=analysis, occurrence_count=1, is_new=True)

    def _call_llm(self, process_name: str, exc: BaseException) -> str | None:
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
        prompt = (
            f"自動売買システムの「{process_name}」処理で例外が発生しました。"
            "与えられたトレースバックのみを根拠に、日本語で短く原因を分析してください。"
            "出力は「推定原因」「影響範囲」「調査・対処の提案」の3見出し、各見出し2項目以内としてください。"
            "断定できない場合は推測であることを明記し、コードの自動修正は行わず提案に留めてください。\n\n"
            f"例外種別: {type(exc).__name__}\n"
            f"メッセージ: {str(exc)[:500]}\n"
            f"トレースバック:\n{tb_text}"
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
                        {"role": "system", "content": "あなたは自動売買システムの障害調査担当です。"},
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
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc_llm:
            logger.warning("例外原因のLLM分析に失敗しました: %s", exc_llm)
            return None

    @staticmethod
    def _is_minor_exception(exc: BaseException) -> bool:
        """リトライで解決しうる想定内の例外はLLMに投げず、キルスイッチ級/未知の例外だけを分析対象にする。"""
        mro_names = {cls.__name__ for cls in type(exc).__mro__}
        return bool(mro_names & set(config.LLM_ERROR_ANALYSIS_SKIP_EXCEPTION_TYPES))

    @staticmethod
    def _fingerprint(exc: BaseException) -> str:
        """例外クラス名＋発生箇所（上位フレームのモジュール・関数）＋正規化メッセージで同一原因を識別する。"""
        frames = traceback.extract_tb(exc.__traceback__)
        top_frames = frames[-_FRAME_DEPTH:] if frames else []
        frame_signature = ";".join(f"{Path(frame.filename).stem}:{frame.name}" for frame in top_frames)
        normalized_message = _normalize_message(str(exc))
        key = "|".join([type(exc).__name__, frame_signature, normalized_message])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _persist(state: dict) -> None:
        now = datetime.now()
        cutoff = now - timedelta(days=_STATE_RETENTION_DAYS)
        pruned = {}
        for fingerprint, entry in state.items():
            last_seen = OpenAIErrorAnalyzer._parse_iso(entry.get("last_seen"))
            if last_seen is not None and last_seen >= cutoff:
                pruned[fingerprint] = entry
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json(_STATE_FILE, pruned)


def create_error_analyzer() -> OpenAIErrorAnalyzer | None:
    if not config.LLM_ERROR_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIErrorAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_ERROR_ANALYSIS_MODEL,
        api_url=config.LLM_API_URL,
        cooldown_minutes=config.LLM_ERROR_ANALYSIS_COOLDOWN_MINUTES,
    )
