"""日経225のADX分布と実現ボラティリティとの関係を分析します。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.domain.market_regime import (
    MarketRegime,
    MarketRegimeThresholds,
    classify_realized_volatility,
)
from src.domain.market_trend import calculate_adx_series
from src.domain.market_volatility import (
    DatedClose,
    calculate_realized_volatility_series,
    summarize_distribution,
)
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient


def _summary(values: list[float]) -> dict:
    return summarize_distribution(values, bin_width=5.0)


def build_report(
    bars,
    symbol: str,
    period: int,
    data_range: str,
    realized_volatility_window: int = 20,
    thresholds: MarketRegimeThresholds | None = None,
) -> dict:
    thresholds = thresholds or MarketRegimeThresholds()
    adx_points = calculate_adx_series(bars, period)
    volatility_points = calculate_realized_volatility_series(
        [DatedClose(bar.date, bar.close) for bar in bars],
        realized_volatility_window,
    )
    adx_by_date = {point.date: point.value for point in adx_points}
    volatility_by_date = {
        point.date: point.value_percent for point in volatility_points
    }

    grouped_values: dict[str, list[float]] = {
        MarketRegime.NORMAL.value: [],
        MarketRegime.CAUTION.value: [],
        MarketRegime.DANGER.value: [],
    }
    for target_date, adx in adx_by_date.items():
        volatility = volatility_by_date.get(target_date)
        if volatility is not None:
            level = classify_realized_volatility(volatility, thresholds)
            grouped_values[level.value].append(adx)

    filtered_values = grouped_values[MarketRegime.CAUTION.value] + grouped_values[
        MarketRegime.DANGER.value
    ]
    return {
        "symbol": symbol,
        "data_range": data_range,
        "period": period,
        "smoothing_method": "Wilder",
        "realized_volatility_window": realized_volatility_window,
        "realized_volatility_thresholds": {
            "caution": thresholds.realized_vol_caution,
            "danger": thresholds.realized_vol_danger,
        },
        "value_unit": "index",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "adx": [
            {"date": point.date.isoformat(), "value": point.value}
            for point in adx_points
        ],
        "adx_summary": _summary([point.value for point in adx_points]),
        "realized_volatility_filter": {
            "caution_or_danger": {
                "count": len(filtered_values),
                "adx_summary": _summary(filtered_values),
            },
            "caution": {
                "count": len(grouped_values[MarketRegime.CAUTION.value]),
                "adx_summary": _summary(grouped_values[MarketRegime.CAUTION.value]),
            },
            "danger": {
                "count": len(grouped_values[MarketRegime.DANGER.value]),
                "adx_summary": _summary(grouped_values[MarketRegime.DANGER.value]),
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="日経225のADX分布を分析します")
    parser.add_argument("--period", type=int, default=14, help="ADX期間（日数）")
    parser.add_argument("--volatility-window", type=int, default=20, help="実現ボラティリティの窓（日数）")
    parser.add_argument("--range", dest="data_range", default="10y", help="Yahoo Financeの取得範囲（例: 10y, max）")
    parser.add_argument("--output", type=Path, default=None, help="JSON出力先")
    args = parser.parse_args()
    if args.period <= 0:
        raise SystemExit("--period は正数で指定してください")
    if args.volatility_window <= 0:
        raise SystemExit("--volatility-window は正数で指定してください")

    symbol = "^N225"
    bars = YahooIndexClient().get_daily_ohlc(symbol, range_=args.data_range)
    if not bars:
        raise SystemExit("日経225の日足データを取得できませんでした")

    output = args.output or Path(__file__).resolve().parents[2] / "data" / "analysis" / "adx_analysis_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            build_report(
                bars,
                symbol,
                args.period,
                args.data_range,
                args.volatility_window,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
