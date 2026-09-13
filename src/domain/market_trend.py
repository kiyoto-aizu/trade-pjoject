"""市場指数の日足OHLCからトレンド指標を計算します。"""
from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Sequence

from src.domain.market_volatility import MarketDailyBar


@dataclass(frozen=True)
class AdxPoint:
    date: date
    value: float


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("ADX期間は正数で指定してください")


def _validate_bars(bars: Sequence[MarketDailyBar]) -> None:
    previous_date: date | None = None
    for bar in bars:
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not isfinite(price) or price <= 0 for price in prices):
            raise ValueError("日足の価格は正の有限値で指定してください")
        if bar.high < bar.low:
            raise ValueError("日足の高値は安値以上で指定してください")
        if previous_date is not None and bar.date <= previous_date:
            raise ValueError("日足データは日付昇順で重複なく指定してください")
        previous_date = bar.date


def _directional_movement(previous: MarketDailyBar, current: MarketDailyBar) -> tuple[float, float]:
    upward = current.high - previous.high
    downward = previous.low - current.low
    return (
        upward if upward > downward and upward > 0 else 0.0,
        downward if downward > upward and downward > 0 else 0.0,
    )


def _true_range(previous: MarketDailyBar, current: MarketDailyBar) -> float:
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def calculate_adx_series(
    bars: Sequence[MarketDailyBar],
    period: int = 14,
) -> list[AdxPoint]:
    """Wilder平滑化によるADXの時系列を返します。

    最初のperiod本の+DM、-DM、TRを合計して初期化し、以後は
    ``previous - previous / period + current``で更新します。ADX自体も
    最初のperiod個のDXの平均で初期化します。
    """
    _validate_period(period)
    _validate_bars(bars)
    if len(bars) < period * 2 + 1:
        return []

    directional_movements = [
        (*_directional_movement(previous, current), _true_range(previous, current))
        for previous, current in zip(bars, bars[1:])
    ]
    plus_sum = sum(item[0] for item in directional_movements[:period])
    minus_sum = sum(item[1] for item in directional_movements[:period])
    true_range_sum = sum(item[2] for item in directional_movements[:period])

    dx_values: list[tuple[int, float]] = []
    for movement_index, (plus_dm, minus_dm, true_range) in enumerate(
        directional_movements[period:], start=period
    ):
        plus_sum = plus_sum - plus_sum / period + plus_dm
        minus_sum = minus_sum - minus_sum / period + minus_dm
        true_range_sum = true_range_sum - true_range_sum / period + true_range
        if true_range_sum == 0:
            dx = 0.0
        else:
            plus_di = 100.0 * plus_sum / true_range_sum
            minus_di = 100.0 * minus_sum / true_range_sum
            denominator = plus_di + minus_di
            dx = 100.0 * abs(plus_di - minus_di) / denominator if denominator else 0.0
        dx_values.append((movement_index, dx))

    if len(dx_values) < period:
        return []

    adx = sum(value for _, value in dx_values[:period]) / period
    points = [AdxPoint(bars[dx_values[period - 1][0] + 1].date, adx)]
    for movement_index, dx in dx_values[period:]:
        adx = adx - adx / period + dx / period
        points.append(AdxPoint(bars[movement_index + 1].date, adx))
    return points


def calculate_adx(
    bars: Sequence[MarketDailyBar],
    period: int = 14,
) -> float | None:
    """OHLC末日のADXを返します。データ不足時はNoneを返します。"""
    points = calculate_adx_series(bars, period)
    return points[-1].value if points else None
