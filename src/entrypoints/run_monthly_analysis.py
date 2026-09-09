import argparse
import json
import logging
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path

from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer
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
    summaries = []
    for path in sorted(report_directory.glob(f"{month_text}-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("日次レポートを読み込めません: %s (%s)", path, exc)
            continue
        if isinstance(data, dict):
            summaries.append({
                "date": data.get("date", path.stem),
                "trading_mode": data.get("trading_mode"),
                "order_count": data.get("order_count", 0),
                "total_profit_loss": data.get("total_profit_loss", 0),
                "kill_switch_triggered": data.get("kill_switch_triggered", False),
            })
    return summaries


def _load_backtest_summaries(backtest_directory: Path, start: date, end: date) -> list[dict]:
    summaries = []
    for path in sorted(backtest_directory.glob("latest_timeseries_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at")
            generated_date = datetime.fromisoformat(generated_at).date() if generated_at else date.fromtimestamp(path.stat().st_mtime)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("バックテスト結果を読み込めません: %s (%s)", path, exc)
            continue
        if start <= generated_date <= end and isinstance(data, dict):
            summaries.append({
                "generated_at": generated_date.isoformat(),
                "period_start": data.get("period_start"),
                "period_end": data.get("period_end"),
                "total_pnl": data.get("total_pnl", data.get("総損益", 0)),
                "total_trades": data.get("total_trades", data.get("総取引数", 0)),
                "win_rate": data.get("win_rate", data.get("勝率", 0)),
                "max_drawdown": data.get("max_drawdown", data.get("最大ドローダウン", 0)),
                "final_position": data.get("final_position", data.get("最終保有数", 0)),
            })
    return summaries


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
    with process_notification("月次総合分析", notify_lifecycle=False):
        summary = build_monthly_summary(month_text, args.reports, args.backtests)
        analyzer = create_daily_analyzer()
        analysis = analyzer.analyze_monthly(summary) if analyzer else None
        result = {**summary, "generated_at": datetime.now().isoformat(timespec="seconds"), "llm_analysis": analysis}
        args.output.mkdir(parents=True, exist_ok=True)
        output_path = args.output / f"{month_text}.json"
        write_json(output_path, result)
        lines = [
            "【月次総合分析】",
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