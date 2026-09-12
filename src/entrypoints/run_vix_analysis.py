"""VIXの分布と日経225実現ボラティリティとの相関を分析します。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.domain.market_volatility import (
    DatedClose,
    calculate_pearson_correlation,
    calculate_previous_day_changes,
    calculate_realized_volatility_series,
    summarize_distribution,
)
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient


def build_report(vix_bars, nikkei_bars, window: int, data_range: str) -> dict:
    vix_closes = [DatedClose(bar.date, bar.close) for bar in vix_bars]
    nikkei_closes = [DatedClose(bar.date, bar.close) for bar in nikkei_bars]
    vix_changes = calculate_previous_day_changes(vix_closes)
    realized_volatility = calculate_realized_volatility_series(nikkei_closes, window)
    vix_by_date = {point.date: point.close for point in vix_closes}
    correlation_pairs = [
        (vix_by_date[point.date], point.value_percent)
        for point in realized_volatility
        if point.date in vix_by_date
    ]
    return {
        "symbol": "^VIX",
        "related_symbol": "^N225",
        "data_range": data_range,
        "window": window,
        "annualization_days": 252,
        "value_unit": "percent",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vix": [
            {"date": point.date.isoformat(), "value": point.close}
            for point in vix_closes
        ],
        "daily_change": [
            {"date": point.date.isoformat(), "value_percent": point.value_percent}
            for point in vix_changes
        ],
        "vix_summary": summarize_distribution([point.close for point in vix_closes]),
        "daily_change_summary": summarize_distribution(
            [point.value_percent for point in vix_changes]
        ),
        "correlation": {
            "metric_x": "vix_close",
            "metric_y": "nikkei225_realized_volatility",
            "count": len(correlation_pairs),
            "pearson": calculate_pearson_correlation(correlation_pairs),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="VIXの分布と相関を分析します")
    parser.add_argument("--window", type=int, default=20, help="実現ボラティリティの窓（日数）")
    parser.add_argument("--range", dest="data_range", default="10y", help="Yahoo Financeの取得範囲（例: 10y, max）")
    parser.add_argument("--output", type=Path, default=None, help="JSON出力先")
    args = parser.parse_args()
    if args.window <= 0:
        raise SystemExit("--window は正数で指定してください")

    client = YahooIndexClient()
    vix_bars = client.get_daily_ohlc("^VIX", range_=args.data_range)
    nikkei_bars = client.get_daily_ohlc("^N225", range_=args.data_range)
    if not vix_bars:
        raise SystemExit("VIXの日足データを取得できませんでした")
    if not nikkei_bars:
        raise SystemExit("日経225の日足データを取得できませんでした")

    output = args.output or Path(__file__).resolve().parents[2] / "data" / "analysis" / "vix_analysis_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_report(vix_bars, nikkei_bars, args.window, args.data_range), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()