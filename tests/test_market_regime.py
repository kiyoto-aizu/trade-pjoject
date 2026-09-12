from datetime import date, timedelta

import pytest

from src.application.market_regime_usecase import MarketRegimeUseCase
from src.domain.market_regime import (
    MarketRegime,
    MarketRegimeThresholds,
    calculate_market_regime,
    classify_realized_volatility,
    classify_vix,
)
from src.domain.market_volatility import MarketDailyBar


THRESHOLDS = MarketRegimeThresholds()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (16.99, MarketRegime.NORMAL),
        (17.0, MarketRegime.CAUTION),
        (28.99, MarketRegime.CAUTION),
        (29.0, MarketRegime.DANGER),
    ],
)
def test_classify_realized_volatility_uses_inclusive_boundaries(value, expected):
    assert classify_realized_volatility(value, THRESHOLDS) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (16.99, MarketRegime.NORMAL),
        (17.0, MarketRegime.CAUTION),
        (26.99, MarketRegime.CAUTION),
        (27.0, MarketRegime.DANGER),
    ],
)
def test_classify_vix_uses_inclusive_boundaries(value, expected):
    assert classify_vix(value, THRESHOLDS) == expected


def test_market_regime_uses_the_more_severe_level():
    assert calculate_market_regime(30.0, 30.0, 0.5, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(30.0, 12.0, 0.5, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(18.0, 10.0, 0.5, THRESHOLDS) == MarketRegime.CAUTION
    assert calculate_market_regime(10.0, 28.0, 0.5, THRESHOLDS) == MarketRegime.DANGER


@pytest.mark.parametrize(
    ("base_regime_values", "expected"),
    [
        ((10.0, 10.0), MarketRegime.CAUTION),
        ((18.0, 10.0), MarketRegime.DANGER),
        ((30.0, 10.0), MarketRegime.DANGER),
    ],
)
def test_nikkei_change_upgrades_by_one_level(base_regime_values, expected):
    assert calculate_market_regime(*base_regime_values, -2.0, THRESHOLDS) == expected


def test_positive_nikkei_change_also_upgrades_by_one_level():
    assert calculate_market_regime(10.0, 10.0, 2.0, THRESHOLDS) == MarketRegime.CAUTION


def test_change_below_upgrade_threshold_does_not_upgrade():
    assert calculate_market_regime(10.0, 10.0, 1.99, THRESHOLDS) == MarketRegime.NORMAL


def test_missing_classification_input_is_safe_side_danger():
    assert classify_realized_volatility(None, THRESHOLDS) == MarketRegime.DANGER
    assert classify_vix(None, THRESHOLDS) == MarketRegime.DANGER
    assert calculate_market_regime(10.0, None, None, THRESHOLDS) == MarketRegime.DANGER


def test_thresholds_reject_invalid_order():
    with pytest.raises(ValueError):
        MarketRegimeThresholds(realized_vol_caution=30.0, realized_vol_danger=29.0)
    with pytest.raises(ValueError):
        MarketRegimeThresholds(vix_caution=28.0, vix_danger=27.0)


class FakeMarketDataClient:
    def __init__(self, nikkei_bars=None, vix_bars=None):
        self.nikkei_bars = nikkei_bars or []
        self.vix_bars = vix_bars or []

    def get_daily_ohlc(self, symbol, range_="3mo"):
        if symbol == "^N225":
            return self.nikkei_bars
        if symbol == "^VIX":
            return self.vix_bars
        raise AssertionError(f"unexpected symbol: {symbol}")


def _bars(closes):
    start = date(2026, 1, 1)
    return [
        MarketDailyBar(start + timedelta(days=index), close, close, close, close)
        for index, close in enumerate(closes)
    ]


def test_use_case_returns_latest_market_regime_values():
    nikkei = _bars([100.0 + index for index in range(22)])
    vix = _bars([20.0])
    result = MarketRegimeUseCase(
        FakeMarketDataClient(nikkei, vix), THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert result.data_available is True
    assert result.regime == MarketRegime.CAUTION
    assert result.vix == 20.0
    assert result.nikkei_change_percent == pytest.approx((121 / 120 - 1) * 100)
    assert result.failure_reason is None


def test_use_case_falls_back_to_danger_when_data_is_unavailable():
    result = MarketRegimeUseCase(
        FakeMarketDataClient(), THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert result.failure_reason


def test_use_case_falls_back_to_danger_when_client_raises():
    class FailingClient:
        def get_daily_ohlc(self, symbol, range_="3mo"):
            raise TimeoutError("Yahoo unavailable")

    result = MarketRegimeUseCase(
        FailingClient(), THRESHOLDS, realized_volatility_window=20
    ).execute()

    assert result.regime == MarketRegime.DANGER
    assert result.data_available is False
    assert "Yahoo unavailable" in result.failure_reason