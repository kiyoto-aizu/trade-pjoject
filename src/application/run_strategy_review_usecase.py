from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable

from src.infrastructure.notification.slack_notify import (
    format_result_notification,
    notify_analysis,
)

logger = logging.getLogger(__name__)

AdvisorOperation = Callable[[date, date], dict[str, Any]]
VerificationOperation = Callable[[Path], dict[str, Any]]
NotificationOperation = Callable[[str], bool]

_VERDICTS = ("支持", "棄却", "追加データ必要")


def _display_path(value: object) -> str:
    path = Path(value)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


class RunStrategyReviewUseCase:
    """既存の仮説生成・検証処理を順に呼び、件数と保存先だけを通知する。"""

    def __init__(
        self,
        advisor: AdvisorOperation,
        verifier: VerificationOperation,
        notifier: NotificationOperation = notify_analysis,
        summarizer: ReviewSummaryOperation | None = None,
    ):
        self.advisor = advisor
        self.verifier = verifier
        self.notifier = notifier
        self.summarizer = summarizer

    def _notify(self, message: str) -> None:
        try:
            if not self.notifier(message):
                logger.warning("戦略レビューのSlack通知に失敗しました")
        except Exception:
            logger.exception("戦略レビューのSlack通知で例外が発生しました")

    def _notify_failure(self, stage: str, exc: Exception, advisor_path: object | None = None) -> None:
        details = [f"失敗箇所: {stage}", f"エラー種別: {type(exc).__name__}"]
        if advisor_path is not None:
            details.append(f"仮説ファイル: {_display_path(advisor_path)}")
        details.append("検証完了前のため、成功結果としては扱っていません。詳細は実行ログを確認してください。")
        message = format_result_notification(
            "戦略レビュー",
            "戦略仮説アドバイザー+仮説検証役",
            f"戦略レビューが{stage}で失敗しました。",
            details,
        )
        self._notify(message)

    def execute(self, start: date, end: date) -> dict[str, Any]:
        if start > end:
            raise ValueError("開始日は終了日以前の日付を指定してください")
        started_at = time.perf_counter()
        try:
            advisor_result = self.advisor(start, end)
        except Exception as exc:
            logger.exception("戦略仮説アドバイザーに失敗しました")
            self._notify_failure("仮説生成", exc)
            raise

        advisor_path = advisor_result["output_path"]
        try:
            verification_result = self.verifier(Path(advisor_path))
        except Exception as exc:
            logger.exception("仮説検証役に失敗しました")
            self._notify_failure("仮説検証", exc, advisor_path)
            raise

        results = verification_result["results"]
        judged_results = [result for result in results if result.get("status") == "判定済み"]
        unverifiable_count = sum(result.get("status") == "検証不能" for result in results)
        untested_count = sum(
            str(result.get("status", "")).startswith("未検証(") for result in results
        )
        counts = Counter(result.get("verdict") for result in judged_results)
        hypothesis_count = len(results)
        verdict_summary = (
            f"支持{counts.get('支持', 0)}件/"
            f"棄却{counts.get('棄却', 0)}件/"
            f"追加データ必要{counts.get('追加データ必要', 0)}件"
        )
        if unverifiable_count or untested_count:
            progress = "検証未完了" if not judged_results else "検証一部未完了"
            summary = (
                f"仮説{hypothesis_count}件生成、{progress}"
                f"(判定済み{len(judged_results)}件: {verdict_summary}/"
                f"検証不能{unverifiable_count}件/未検証{untested_count}件)"
            )
        else:
            summary = (
                f"仮説{hypothesis_count}件生成、検証完了"
                f"(判定済み{len(judged_results)}件: {verdict_summary}/検証不能0件)"
            )

        summary_data = {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "counts": {
                "total": hypothesis_count,
                "judged": len(judged_results),
                "supported": counts.get("支持", 0),
                "rejected": counts.get("棄却", 0),
                "additional_data_needed": counts.get("追加データ必要", 0),
                "unverifiable": unverifiable_count,
                "untested_priority_excluded": untested_count,
            },
            "hypotheses": [
                {
                    key: result.get(key)
                    for key in ("id", "title", "priority", "status", "verdict", "confidence", "reason", "evidence")
                }
                for result in results
            ],
        }
        review_summary = None
        summary_path = None
        summary_failed = False
        if self.summarizer is not None:
            try:
                review_summary = self.summarizer(summary_data)
                if not isinstance(review_summary, str) or not review_summary.strip():
                    raise ValueError("総括LLMから空の応答が返りました")
                review_summary = review_summary.strip()
                summary_path = Path(verification_result["run_directory"]) / "strategy_review_summary.md"
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(review_summary + "\n", encoding="utf-8")
            except Exception:
                summary_failed = True
                review_summary = None
                summary_path = None
                logger.exception("戦略レビュー総括を生成できませんでした。件数サマリーで通知を続行します")

        duration = time.perf_counter() - started_at
        details = [
            f"仮説ファイル: {_display_path(advisor_path)}",
            f"検証結果: {_display_path(verification_result['run_directory'])}",
            f"フィードバック: {_display_path(verification_result['feedback_path'])}",
        ]
        if review_summary is not None:
            details.extend([
                "【次のアクション案】",
                self._short_summary(review_summary),
                f"総括全文: {_display_path(summary_path)}",
            ])
        elif summary_failed:
            details.append("総括LLMは利用できませんでした。上記の件数・判定結果のみ通知しています。")
        details.append(f"処理時間: {duration:.1f}秒")
        notification = format_result_notification(
            "戦略レビュー",
            "戦略仮説アドバイザー+仮説検証役",
            summary,
            details,
        )
        self._notify(notification)
        return {
            "advisor_result": advisor_result,
            "verification_result": verification_result,
            "hypothesis_count": hypothesis_count,
            "judged_count": len(judged_results),
            "unverifiable_count": unverifiable_count,
            "untested_count": untested_count,
            "verdict_counts": {verdict: counts.get(verdict, 0) for verdict in _VERDICTS},
            "elapsed_seconds": duration,
            "review_summary": review_summary,
            "summary_path": summary_path,
            "summary_failed": summary_failed,
            "notification": notification,
        }

    @staticmethod
    def _short_summary(summary: str, limit: int = 700) -> str:
        text = "\n".join(line.strip() for line in summary.splitlines() if line.strip())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"