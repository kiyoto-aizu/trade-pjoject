import argparse
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.analysis.summary_loader import load_backtest_summaries, load_daily_summaries
from src.infrastructure.notification.line_notify import format_result_notification, process_notification, send_line_notify
from src.infrastructure.persistence.storage import write_json
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.infrastructure.notification.line_notify import process_notification, send_line_notify
from src.infrastructure.notification.slack_notify import notify_analysis

def _week_bounds(reference_date: date) -> tuple[date, date]:
    week_start = reference_date - timedelta(days=reference_date.weekday())
    return week_start, week_start + timedelta(days=4)


def build_weekly_summary(
    week_start: date, week_end: date, report_directory: Path, backtest_directory: Path,
    filter_decision_repository: FilterDecisionRepository | None = None,
) -> dict:
    daily = load_daily_summaries(report_directory, week_start, week_end)
    backtests = load_backtest_summaries(backtest_directory, week_start, week_end)
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "daily": {
            "report_count": len(daily),
            "order_count": sum(int(item["order_count"] or 0) for item in daily),
            "total_profit_loss": round(sum(float(item["total_profit_loss"] or 0) for item in daily), 2),
            "kill_switch_days": sum(1 for item in daily if item["kill_switch_triggered"]),
            "reports": daily,
        },
        "backtest": {
            "run_count": len(backtests),
            "total_pnl": round(sum(float(item["total_pnl"] or 0) for item in backtests), 2),
            "total_trades": sum(int(item["total_trades"] or 0) for item in backtests),
            "runs": backtests,
        },
        "filter_decision_events": (
            filter_decision_repository.summarize_finalized_events(week_start, week_end)
            if filter_decision_repository else {"count": 0, "by_event_type": {}}
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="週次のペーパートレード・バックテスト総合分析を実行します")
    parser.add_argument("--week-start", default=None, help="対象週の月曜（YYYY-MM-DD）。省略時は実行日が属する週")
    parser.add_argument("--reports", type=Path, default=Path("data/reports"))
    parser.add_argument("--backtests", type=Path, default=Path("data/backtest"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/weekly"))
    parser.add_argument("--force", action="store_true", help="土曜以外でも実行する")
    args = parser.parse_args()

    today = date.today()
    if not args.force and today.weekday() != 5:
        return
    if args.week_start:
        week_start = date.fromisoformat(args.week_start)
    else:
        week_start, _ = _week_bounds(today)
    week_end = week_start + timedelta(days=4)
    with process_notification("週次分析", notify_lifecycle=False, trigger="土曜または手動実行"):
        summary = build_weekly_summary(
            week_start, week_end, args.reports, args.backtests,
            FilterDecisionRepository(Path("data/filter_decision_events.sqlite3")),
        )
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_weekly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        args.output.mkdir(parents=True, exist_ok=True)
        output_path = args.output / f"{week_start:%Y-%m-%d}_{week_end:%Y-%m-%d}.json"
        write_json(output_path, result)
        lines = [
            f"対象週: {week_start}～{week_end}",
            f"ペーパートレード: {summary['daily']['report_count']}日 / {summary['daily']['order_count']}件",
            f"バックテスト: {summary['backtest']['run_count']}回 / 損益 {summary['backtest']['total_pnl']}",
            f"詳細: {output_path}",
        ]
        if analysis:
            lines.extend(["LLM週次評価(参考):", analysis])
        send_line_notify(format_result_notification(
            "分析運用", "週次分析", "週次分析が完了しました。", lines
        ))


if __name__ == "__main__":
    main()