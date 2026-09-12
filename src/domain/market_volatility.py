"""日経225など市場指数の日次ボラティリティ計算。"""
from dataclasses import dataclass
from datetime import date
from math import ceil, floor, sqrt
from statistics import mean, median, stdev
from typing import Sequence


@dataclass(frozen=True)
class MarketDailyBar:
    """市場指数の日足OHLC。"""

    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class DatedClose:
    date: date
    close: float


@dataclass(frozen=True)
class RealizedVolatilityPoint:
    date: date
    value_percent: float


@dataclass(frozen=True)
class DailyChangePoint:
    date: date
    value_percent: float


def _validate_window(window: int) -> None:
    if window <= 0:
        raise ValueError("実現ボラティリティ期間は正数で指定してください")


def _validate_closes(closes: Sequence[float]) -> None:
    if any(price <= 0 for price in closes):
        raise ValueError("終値は正数で指定してください")


def calculate_daily_returns(closes: Sequence[float]) -> list[float]:
    """終値列から日次騰落率(%)を計算します。"""
    _validate_closes(closes)
    return [
        (current / previous - 1.0) * 100.0
        for previous, current in zip(closes, closes[1:])
    ]


def calculate_realized_volatility(
    closes: Sequence[float],
    window: int = 20,
) -> float | None:
    """直近window日の日次騰落率から年率実現ボラ(%)を計算します。"""
    _validate_window(window)
    if len(closes) < window + 1:
        return None
    returns = calculate_daily_returns(closes[-(window + 1):])
    return stdev(returns) * sqrt(252)


def calculate_realized_volatility_series(
    dated_closes: Sequence[DatedClose],
    window: int = 20,
) -> list[RealizedVolatilityPoint]:
    """各日を窓の終端とする年率実現ボラの時系列を返します。"""
    _validate_window(window)
    closes = [item.close for item in dated_closes]
    _validate_closes(closes)
    points = []
    for end_index in range(window, len(dated_closes)):
        value = calculate_realized_volatility(
            closes[end_index - window:end_index + 1],
            window=window,
        )
        if value is not None:
            points.append(RealizedVolatilityPoint(dated_closes[end_index].date, value))
    return points


def calculate_previous_day_changes(
    dated_closes: Sequence[DatedClose],
) -> list[DailyChangePoint]:
    """各営業日の前日比(%)を返します。"""
    closes = [item.close for item in dated_closes]
    _validate_closes(closes)
    returns = calculate_daily_returns(closes)
    return [
        DailyChangePoint(dated_closes[index + 1].date, value)
        for index, value in enumerate(returns)
    ]


def summarize_distribution(values: Sequence[float], bin_width: float = 5.0) -> dict:
    """値列の基本統計、指定パーセンタイル、ヒストグラムを返します。"""
    if bin_width <= 0:
        raise ValueError("ヒストグラムの幅は正数で指定してください")
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "standard_deviation": None,
            "percentiles": {},
            "histogram": [],
        }

    ordered = sorted(values)

    def percentile(percentile_value: float) -> float:
        position = (len(ordered) - 1) * percentile_value / 100.0
        lower = floor(position)
        upper = ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * weight

    bins: dict[float, int] = {}
    for value in values:
        start = floor(value / bin_width) * bin_width
        bins[start] = bins.get(start, 0) + 1
    return {
        "count": len(values),
        "mean": mean(values),
        "median": median(values),
        "standard_deviation": stdev(values) if len(values) >= 2 else None,
        "percentiles": {
            f"p{percentile_value}": percentile(percentile_value)
            for percentile_value in (10, 25, 50, 75, 90, 95)
        },
        "histogram": [
            {"from": start, "to": start + bin_width, "count": count}
            for start, count in sorted(bins.items())
        ],
    }


def calculate_pearson_correlation(
    pairs: Sequence[tuple[float, float]],
) -> float | None:
    """対応する値の組からPearson相関係数を計算します。"""
    if len(pairs) < 2:
        return None
    first_values = [pair[0] for pair in pairs]
    second_values = [pair[1] for pair in pairs]
    first_mean = mean(first_values)
    second_mean = mean(second_values)
    numerator = sum(
        (first - first_mean) * (second - second_mean)
        for first, second in pairs
    )
    first_variance = sum((first - first_mean) ** 2 for first in first_values)
    second_variance = sum((second - second_mean) ** 2 for second in second_values)
    denominator = sqrt(first_variance * second_variance)
    return numerator / denominator if denominator else None
