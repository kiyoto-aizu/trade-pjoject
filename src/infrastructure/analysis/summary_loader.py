import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def load_daily_summaries(report_directory: Path, start: date, end: date) -> list[dict]:
    summaries = []
    for path in sorted(report_directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("日次レポートを読み込めません: %s (%s)", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        report_date_text = data.get("date", path.stem)
        try:
            report_date = date.fromisoformat(str(report_date_text)[:10])
        except ValueError:
            continue
        if not start <= report_date <= end:
            continue
        summaries.append({
            "date": data.get("date", path.stem),
            "trading_mode": data.get("trading_mode"),
            "order_count": data.get("order_count", 0),
            "total_profit_loss": data.get("total_profit_loss", 0),
            "kill_switch_triggered": data.get("kill_switch_triggered", False),
        })
    return summaries


def load_backtest_summaries(backtest_directory: Path, start: date, end: date) -> list[dict]:
    summaries = []
    for path in sorted(backtest_directory.glob("latest_timeseries_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            generated_at = data.get("generated_at")
            generated_date = (
                datetime.fromisoformat(generated_at).date()
                if generated_at
                else date.fromtimestamp(path.stat().st_mtime)
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("バックテスト結果を読み込めません: %s (%s)", path, exc)
            continue
        period_start = _parse_date(data.get("period_start")) if isinstance(data, dict) else None
        period_end = _parse_date(data.get("period_end")) if isinstance(data, dict) else None
        has_overlapping_period = (
            period_start is not None
            and period_end is not None
            and period_start <= end
            and start <= period_end
        )
        if (start <= generated_date <= end or has_overlapping_period) and isinstance(data, dict):
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


def _parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None