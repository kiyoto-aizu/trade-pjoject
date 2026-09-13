from datetime import date, timedelta

import pytest

from src.domain.market_trend import calculate_adx, calculate_adx_series
from src.domain.market_volatility import MarketDailyBar


def _bars(closes, spread=1.0):
    start = date(2026, 1, 1)
    return [
        MarketDailyBar(
            start + timedelta(days=index),
            close,
            close + spread,
            close - spread,
            close,
        )
        for index, close in enumerate(closes)
    ]


def test_adx_is_high_for_a_clear_one_directional_trend():
    points = calculate_adx_series(_bars([100 + index * 2 for index in range(30)]), period=5)

    assert points
    assert points[-1].value > 80


def test_adx_is_zero_for_a_flat_market():
    points = calculate_adx_series(_bars([100.0] * 30), period=5)

    assert points
    assert points[-1].value == pytest.approx(0.0)


def test_adx_series_starts_after_wilder_warmup():
    points = calculate_adx_series(_bars([100 + index for index in range(12)]), period=5)

    assert points[0].date == date(2026, 1, 11)
    assert calculate_adx(_bars([100 + index for index in range(9)]), period=5) is None


def test_adx_rejects_invalid_period_and_ohlc():
    with pytest.raises(ValueError):
        calculate_adx_series(_bars([100.0] * 10), period=0)
    with pytest.raises(ValueError):
        calculate_adx_series(
            [MarketDailyBar(date(2026, 1, 1), 100.0, 99.0, 101.0, 100.0)],
            period=1,
        )
    with pytest.raises(ValueError):
        calculate_adx_series(
            [MarketDailyBar(date(2026, 1, 1), 100.0, 101.0, 99.0, 100.0),
             MarketDailyBar(date(2026, 1, 1), 100.0, 101.0, 99.0, 100.0)],
            period=1,
        )
