import pytest

from src.domain.volatility import (
    DailyBar,
    VolatilityLevel,
    adjust_quantity_for_volatility,
    assess_volatility,
    calculate_atr,
    calculate_true_range,
    is_atr_stop_loss_triggered,
    stop_loss_multiplier,
)
from src.domain.models import PriceLimit, TradeSignal
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


@pytest.mark.parametrize(
    ("level", "multiplier"),
    [
        (VolatilityLevel.NORMAL, 1.5),
        (VolatilityLevel.CAUTION, 1.0),
        (VolatilityLevel.DANGER, 0.7),
    ],
)
def test_atr_stop_loss_uses_level_specific_multiplier(level, multiplier):
    assert stop_loss_multiplier(level, 1.5, 1.0, 0.7) == multiplier
    stop_price = 100.0 - 10.0 * multiplier
    assert is_atr_stop_loss_triggered(stop_price, 100.0, 10.0, level, 1.5, 1.0, 0.7)
    assert not is_atr_stop_loss_triggered(stop_price + 0.01, 100.0, 10.0, level, 1.5, 1.0, 0.7)


def test_trade_signal_combines_regular_exit_and_atr_stop_with_or():
    limit = PriceLimit(lower_band=95.0, upper_band=105.0)

    regular_only = TradeSignal.evaluate("7203", 94.0, limit, 40.0, 55.0, 45.0)
    atr_only = TradeSignal.evaluate("7203", 94.0, limit, 50.0, 55.0, 45.0, 100.0, 5.0, 1.0)
    both = TradeSignal.evaluate("7203", 94.0, limit, 40.0, 55.0, 45.0, 100.0, 5.0, 1.0)
    neither = TradeSignal.evaluate("7203", 101.0, limit, 50.0, 55.0, 45.0, 100.0, 5.0, 1.0)

    assert regular_only is not None
    assert atr_only is not None
    assert both is not None
    assert neither is None