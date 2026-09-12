"""日経225の実現ボラティリティと前日比の分布分析。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.domain.market_volatility import (
    DatedClose,
    calculate_previous_day_changes,
    calculate_realized_volatility_series,
    summarize_distribution,
)
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient


def _summary(values: list[float], bin_width: float = 5.0) -> dict:
    return summarize_distribution(values, bin_width)


def build_report(bars, symbol: str, window: int, data_range: str) -> dict:
    dated_closes = [DatedClose(bar.date, bar.close) for bar in bars]
    volatility_points = calculate_realized_volatility_series(dated_closes, window)
    change_points = calculate_previous_day_changes(dated_closes)
    volatility_values = [point.value_percent for point in volatility_points]
    change_values = [point.value_percent for point in change_points]
    return {
        "symbol": symbol,
        "data_range": data_range,
        "window": window,
        "annualization_days": 252,
        "value_unit": "percent",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "realized_volatility": [
            {"date": point.date.isoformat(), "value_percent": point.value_percent}
            for point in volatility_points
        ],
        "daily_change": [
            {"date": point.date.isoformat(), "value_percent": point.value_percent}
            for point in change_points
        ],
        "realized_volatility_summary": _summary(volatility_values),
        "daily_change_summary": _summary(change_values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="日経225の実現ボラティリティ分布を分析します")
    parser.add_argument("--window", type=int, default=20, help="実現ボラティリティの窓（日数）")
    parser.add_argument("--range", dest="data_range", default="max", help="Yahoo Financeの取得範囲（例: 10y, max）")
    parser.add_argument("--output", type=Path, default=None, help="JSON出力先")
    args = parser.parse_args()
    if args.window <= 0:
        raise SystemExit("--window は正数で指定してください")

    symbol = "^N225"
    bars = YahooIndexClient().get_daily_ohlc(symbol, range_=args.data_range)
    if not bars:
        raise SystemExit("日経225の日足データを取得できませんでした")

    output = args.output or Path(__file__).resolve().parents[2] / "data" / "analysis" / "market_volatility_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_report(bars, symbol, args.window, args.data_range), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
