import argparse
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.analysis.summary_loader import (
    build_daily_period_comparison,
    build_period_metadata,
    load_backtest_summaries,
    load_daily_summaries,
    summarize_backtest_runs,
    summarize_daily_reports,
)
from src.infrastructure.notification.slack_notify import format_result_notification, notify_analysis, process_notification
from src.infrastructure.persistence.storage import write_json
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

logger = logging.getLogger(__name__)


def _month_bounds(month_text: str) -> tuple[date, date]:
    try:
        year, month = (int(value) for value in month_text.split("-"))
        start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"月の形式が不正です: {month_text}（YYYY-MMを指定してください）") from exc
    return start, date(year, month, monthrange(year, month)[1])


def _load_daily_summaries(report_directory: Path, month_text: str) -> list[dict]:
    start, end = _month_bounds(month_text)
    return load_daily_summaries(report_directory, start, end)


def _load_backtest_summaries(backtest_directory: Path, start: date, end: date) -> list[dict]:
    return load_backtest_summaries(backtest_directory, start, end)


def build_monthly_summary(
    month_text: str, report_directory: Path, backtest_directory: Path,
    filter_decision_repository: FilterDecisionRepository | None = None,
    as_of: date | None = None,
) -> dict:
    start, end = _month_bounds(month_text)
    as_of = as_of or date.today()
    daily = _load_daily_summaries(report_directory, month_text)
    previous_month_end = start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    previous_daily = load_daily_summaries(
        report_directory, previous_month_start, previous_month_end
    )
    backtests = _load_backtest_summaries(backtest_directory, start, end)
    period = build_period_metadata(start, end, as_of)
    daily_summary = summarize_daily_reports(daily)
    previous_daily_summary = summarize_daily_reports(previous_daily)
    return {
        "month": month_text,
        "period": period,
        "daily": daily_summary,
        "comparison": build_daily_period_comparison(
            period,
            daily_summary,
            build_period_metadata(previous_month_start, previous_month_end, previous_month_end),
            previous_daily_summary,
        ),
        "backtest": summarize_backtest_runs(backtests, start, end),
        "filter_decision_events": (
            filter_decision_repository.summarize_finalized_events(start, end)
            if filter_decision_repository else {"count": 0, "by_event_type": {}}
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="月次のペーパートレード・バックテスト総合分析を実行します")
    parser.add_argument("--month", default=None, help="対象月（YYYY-MM）。省略時は当月")
    parser.add_argument("--reports", type=Path, default=Path("data/reports"))
    parser.add_argument("--backtests", type=Path, default=Path("data/backtest"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/monthly"))
    parser.add_argument("--force", action="store_true", help="月末以外でも実行する")
    args = parser.parse_args()

    today = date.today()
    month_text = args.month or f"{today.year:04d}-{today.month:02d}"
    if not args.force and today != _month_bounds(f"{today.year:04d}-{today.month:02d}")[1]:
        logger.info("月末ではないため月次分析をスキップします")
        return
    with process_notification("月次総合分析", notify_lifecycle=False, trigger="月末または手動実行"):
        summary = build_monthly_summary(
            month_text, args.reports, args.backtests,
            FilterDecisionRepository(Path("data/filter_decision_events.sqlite3")), today,
        )
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_monthly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        args.output.mkdir(parents=True, exist_ok=True)
        output_path = args.output / f"{month_text}.json"
        write_json(output_path, result)
        backtest = summary["backtest"]
        backtest_detail = (
            f"{backtest['run_count']}回 / 損益 {backtest['total_pnl']}"
            if backtest["exact_period_run_available"]
            else f"{backtest['run_count']}回 / 対象期間一致 {backtest['matching_period_run_count']}回"
        )
        lines = [
            f"対象月: {month_text}",
            f"集計状態: {'確定' if summary['period']['is_complete'] else '途中'}",
            f"ペーパートレード: {summary['daily']['report_count']}日 / {summary['daily']['order_count']}件",
            f"バックテスト: {backtest_detail}",
            f"詳細: {output_path}",
        ]
        if analysis:
            lines.extend(["LLM月次評価(参考):", analysis])
        message = format_result_notification(
            "分析運用", "月次総合分析", "月次分析が完了しました。", lines
        )
        notify_analysis(message)


if __name__ == "__main__":
    main()