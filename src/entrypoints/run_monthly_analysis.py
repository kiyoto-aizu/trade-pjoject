import argparse
import logging
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path

from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
from src.infrastructure.analysis.summary_loader import load_backtest_summaries, load_daily_summaries
from src.infrastructure.notification.line_notify import process_notification, send_line_notify
from src.infrastructure.persistence.storage import write_json

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


def build_monthly_summary(month_text: str, report_directory: Path, backtest_directory: Path) -> dict:
    start, end = _month_bounds(month_text)
    daily = _load_daily_summaries(report_directory, month_text)
    backtests = _load_backtest_summaries(backtest_directory, start, end)
    return {
        "month": month_text,
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="月次のペーパートレード・バックテスト総合分析を実行します")
    parser.add_argument("--month", default=None, help="対象月（YYYY-MM）。省略時は前月")
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
        summary = build_monthly_summary(month_text, args.reports, args.backtests)
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_monthly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        args.output.mkdir(parents=True, exist_ok=True)
        output_path = args.output / f"{month_text}.json"
        write_json(output_path, result)
        lines = [
            "【月次総合分析】結果",
            f"対象月: {month_text}",
            f"ペーパートレード: {summary['daily']['report_count']}日 / {summary['daily']['order_count']}件",
            f"バックテスト: {summary['backtest']['run_count']}回 / 損益 {summary['backtest']['total_pnl']}",
            f"詳細: {output_path}",
        ]
        if analysis:
            lines.extend(["--- LLM月次評価（参考） ---", analysis])
        send_line_notify("\n".join(lines))


if __name__ == "__main__":
    main()