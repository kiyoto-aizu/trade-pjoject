import pytest

from src.domain.volatility import (
    DailyBar,
    VolatilityLevel,
    adjust_quantity_for_volatility,
    assess_volatility,
    calculate_atr,
    calculate_true_range,
)
from src.application.backtest_usecase import _adjust_backtest_quantity


def test_true_range_uses_the_largest_of_three_ranges():
    bar = DailyBar(high=110, low=95, close=100)

    assert calculate_true_range(bar, previous_close=130) == 35


def test_atr_is_simple_average_of_recent_true_ranges():
    bars = [
        DailyBar(high=101, low=99, close=100),
        DailyBar(high=104, low=99, close=103),
        DailyBar(high=106, low=102, close=105),
    ]

    assert calculate_atr(bars, period=2) == pytest.approx((5 + 4) / 2)


def test_assess_volatility_classifies_caution_and_danger():
    normal_bars = [DailyBar(high=101, low=99, close=100)] * 3
    caution_bars = normal_bars[:-1] + [DailyBar(high=103, low=97, close=100)]
    danger_bars = normal_bars[:-1] + [DailyBar(high=105, low=95, close=100)]

    assert assess_volatility(caution_bars, period=3).level == VolatilityLevel.CAUTION
    assert assess_volatility(danger_bars, period=3).level == VolatilityLevel.DANGER


def test_assess_volatility_returns_none_when_data_is_insufficient():
    assert assess_volatility([DailyBar(high=101, low=99, close=100)], period=2) is None


def test_quantity_adjustment_skips_danger_and_reduces_caution():
    assert adjust_quantity_for_volatility(300, VolatilityLevel.NORMAL, 100) == 300
    assert adjust_quantity_for_volatility(300, VolatilityLevel.CAUTION, 100) == 100
    assert adjust_quantity_for_volatility(300, VolatilityLevel.DANGER, 100) == 0
    assert adjust_quantity_for_volatility(300, VolatilityLevel.DANGER, 100, danger_action="minimum") == 100


def test_backtest_quantity_uses_the_same_atr_adjustment():
    bars = [DailyBar(high=101, low=99, close=100)] * 13
    bars.append(DailyBar(high=105, low=95, close=100))

    assert _adjust_backtest_quantity(300, bars) == 0