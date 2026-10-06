from datetime import datetime

import pytest

from scripts.analysis import analyze_intraday_movement_and_stagnant_positions as analysis


def test_time_window_boundaries_and_market_break():
    assert analysis.time_window(datetime.fromisoformat("2026-10-05T09:00:00")) == "09:00-09:30"
    assert analysis.time_window(datetime.fromisoformat("2026-10-05T09:29:00")) == "09:00-09:30"
    assert analysis.time_window(datetime.fromisoformat("2026-10-05T09:30:00")) == "09:30-10:00"
    assert analysis.time_window(datetime.fromisoformat("2026-10-05T12:00:00")) is None
    assert analysis.time_window(datetime.fromisoformat("2026-10-05T15:30:00")) is None


def test_stagnant_judgment_uses_strict_less_than_threshold():
    assert analysis.is_stagnant([100.0, 100.9], 5.0, 0.2)
    assert not analysis.is_stagnant([100.0, 101.0], 5.0, 0.2)


def test_virtual_pnl_applies_one_sided_spread_to_sell_price():
    assert analysis.virtual_pnl(100.0, 102.0, 10) == 20.0
    assert analysis.virtual_pnl(100.0, 102.0, 10, 0.001) == pytest.approx(18.98)


def test_trade_positions_matches_later_fifo_sell_and_rejects_zero_price_pnl():
    events = [
        {"symbol": "1234", "side": "2", "price": 100, "qty": 100,
         "timestamp": "2026-09-25T09:00:00", "result_code": 0},
        {"symbol": "1234", "side": "2", "price": 110, "qty": 100,
         "timestamp": "2026-09-28T09:00:00", "result_code": 0},
        {"symbol": "1234", "side": "1", "price": 105, "qty": 200,
         "timestamp": "2026-09-28T15:20:00", "result_code": 0},
        {"symbol": "5678", "side": "2", "price": 20, "qty": 100,
         "timestamp": "2026-10-02T09:00:00", "result_code": 0},
        {"symbol": "5678", "side": "1", "price": 0, "qty": 100,
         "timestamp": "2026-10-02T15:20:00", "result_code": 0},
    ]

    positions = analysis._trade_positions(events, "production")

    assert positions[0]["actual_pnl"] == 500
    assert positions[0]["actual_exit_time"].isoformat() == "2026-09-28T15:20:00"
    assert positions[1]["actual_pnl"] == -500
    assert positions[2]["actual_pnl"] is None