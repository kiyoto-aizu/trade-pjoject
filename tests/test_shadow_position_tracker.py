from datetime import datetime

from src.application.shadow_position_tracker import (
    CHECKPOINT_MINUTES,
    ShadowPosition,
    elapsed_trading_minutes,
    resolve_reference,
)


def _position(buy_at="2026-10-06T10:00:00", **kwargs):
    return ShadowPosition(
        position_key="order-1", symbol="1234", buy_at=datetime.fromisoformat(buy_at),
        buy_price=100.0, buy_quantity=100, reference_value=5.0, reference_kind="daily_atr", **kwargs,
    )


def test_checkpoints_are_emitted_once_at_each_elapsed_trading_minute():
    position = _position()
    for minute in range(1, 61):
        observed_at = datetime.fromisoformat("2026-10-06T10:00:00").replace(minute=minute % 60)
        if minute >= 60:
            observed_at = datetime.fromisoformat("2026-10-06T11:00:00")
        rows = position.observe(observed_at, 100.0 + minute / 100)
        assert len(rows) <= 1
    rows = position.observe(datetime.fromisoformat("2026-10-06T11:01:00"), 101.0)
    assert rows == []
    assert position.completed_checkpoints == set(CHECKPOINT_MINUTES)


def test_lunch_break_is_excluded_and_after_1520_has_no_checkpoint():
    start = datetime.fromisoformat("2026-10-06T11:20:00")
    assert elapsed_trading_minutes(start, datetime.fromisoformat("2026-10-06T12:35:00")) == 15
    position = _position(buy_at="2026-10-06T11:20:00")
    row = position.observe(datetime.fromisoformat("2026-10-06T12:35:00"), 101.0)[0]
    assert row["checkpoint_minutes"] == 15
    assert "checkpoint_observed_late" not in row["data_quality"]
    assert position.observe(datetime.fromisoformat("2026-10-06T15:20:00"), 101.0) == []


def test_reference_falls_back_to_half_percent_of_previous_close():
    assert resolve_reference(None, 200.0) == (1.0, "previous_close_0.5_percent")
    assert resolve_reference(4.0, 200.0) == (4.0, "daily_atr")


def test_invalid_price_and_missing_history_are_quality_flags_without_exceptions():
    position = _position(history_incomplete=True)
    first = position.observe(datetime.fromisoformat("2026-10-06T10:15:00"), 0)[0]
    second = position.observe(datetime.fromisoformat("2026-10-06T10:30:00"), None, "board_unavailable")[0]
    assert first["highest_price"] is None
    assert first["lowest_price"] is None
    assert first["range_ratio"] is None
    assert {"history_missing_after_restart", "price_zero_or_invalid", "few_observations"}.issubset(
        first["data_quality"]
    )
    assert {"history_missing_after_restart", "price_missing", "board_unavailable",
            "few_observations"}.issubset(second["data_quality"])


def test_candidate_skip_after_checkpoint_is_not_looked_ahead():
    position = _position(buy_at="2026-10-06T09:00:00")
    position.capacity_blocked_candidates["5678"] = {
        "count": 1,
        "first_at": "2026-10-06T09:16:00",
        "last_at": "2026-10-06T09:16:00",
    }

    first = position.observe(datetime.fromisoformat("2026-10-06T09:15:00"), 100.0)[0]
    second = position.observe(datetime.fromisoformat("2026-10-06T09:30:00"), 100.0)[0]

    assert first["capacity_blocked_candidates"] == {}
    assert second["capacity_blocked_candidates"]["5678"]["count"] == 1