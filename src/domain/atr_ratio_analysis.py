"""銘柄別ATR倍率の時系列・分布分析。"""
from dataclasses import dataclass
from datetime import date
from math import ceil, floor
from statistics import median
from typing import Sequence

from src.domain.market_volatility import summarize_distribution
from src.domain.volatility import DailyBar, assess_volatility


@dataclass(frozen=True)
class DatedDailyBar:
    """日付付きの確定日足OHLCデータ。"""

    date: date
    bar: DailyBar


@dataclass(frozen=True)
class AtrRatioPoint:
    """日付別のATR倍率。"""

    date: date
    ratio: float


def calculate_atr_ratio_series(
    dated_bars: Sequence[DatedDailyBar],
    period: int = 14,
) -> list[AtrRatioPoint]:
    """各日を終端として既存ATR判定と同じ倍率を計算します。"""
    if period <= 0:
        raise ValueError("ATR期間は正数で指定してください")

    points = []
    bars = [item.bar for item in dated_bars]
    for end_index in range(period - 1, len(dated_bars)):
        assessment = assess_volatility(bars[:end_index + 1], period=period)
        if assessment is not None:
            points.append(AtrRatioPoint(dated_bars[end_index].date, assessment.ratio))
    return points


def calculate_at_or_below_percentile(values: Sequence[float], threshold: float) -> float | None:
    """threshold以下の観測値の割合を百分位で返します。"""
    if not values:
        return None
    return sum(value <= threshold for value in values) / len(values) * 100.0


def summarize_atr_ratio_distribution(values: Sequence[float]) -> dict:
    """ATR倍率の要約統計を返します。"""
    return summarize_distribution(values, bin_width=0.25)


def summarize_symbol_atr_ratios(points_by_symbol: dict[str, Sequence[AtrRatioPoint]]) -> list[dict]:
    """銘柄ごとの観測数・中央値・p90を返します。"""
    summaries = []
    for symbol in sorted(points_by_symbol):
        values = [point.ratio for point in points_by_symbol[symbol]]
        if not values:
            continue
        summary = summarize_atr_ratio_distribution(values)
        summaries.append({
            "symbol": symbol,
            "count": len(values),
            "median": median(values),
            "p90": summary["percentiles"]["p90"],
        })
    return summaries


def calculate_reference_candidates(values: Sequence[float]) -> dict:
    """中央値とp90を、閾値検討の参考値として返します。"""
    summary = summarize_atr_ratio_distribution(values)
    percentiles = summary["percentiles"]
    if not percentiles:
        return {
            "normal_upper_ratio": None,
            "caution_danger_boundary_ratio": None,
            "basis": {
                "normal_upper": "p50",
                "caution_danger_boundary": "p90",
            },
        }
    return {
        "normal_upper_ratio": percentiles["p50"],
        "caution_danger_boundary_ratio": percentiles["p90"],
        "basis": {
            "normal_upper": "p50",
            "caution_danger_boundary": "p90",
        },
    }