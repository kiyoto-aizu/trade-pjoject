from datetime import date, datetime, timedelta

import pytest

from src.domain.models import FilteringResult, MinuteBar
from src.infrastructure.backtest.historical_clients import (
    DatedDailyBar,
    HistoricalBoardClient,
    HistoricalClock,
    HistoricalFilteringResultRepository,
    HistoricalMarketDataClient,
    NoOpDailyAnalyzer,
    NoOpFilterDecisionRepository,
)
from src.infrastructure.market_data.historical_replay import HistoricalClock as CompatibleHistoricalClock
from src.infrastructure.persistence.historical_filtering_result_repository import (
    HistoricalFilteringResultRepository as CompatibleFilteringResultRepository,
)


TRADING_DAY = date(2026, 9, 25)


def _minute_bar(timestamp: str, price: float) -> MinuteBar:
    return MinuteBar(
        time=timestamp,
        price=price,
        cumulative_volume=None,
        volume=None,
        source="yahoo",
    )


def test_historical_clock_is_idempotent_and_advances_once_per_sleep():
    first = datetime(2026, 9, 25, 9, 0)
    second = first + timedelta(minutes=1)
    clock = HistoricalClock(TRADING_DAY, [first, second])

    assert clock.now() == first
    assert clock.now() == first
    clock.advance(60)
    assert clock.now() == second
    clock.advance(60)
    assert clock.now() == datetime(2026, 9, 25, 15, 30, 1)
    clock.advance(60)
    assert clock.now() == datetime(2026, 9, 25, 15, 30, 1)
    assert clock.current_date() == TRADING_DAY


def test_historical_clock_rejects_empty_or_other_day_timestamps():
    with pytest.raises(ValueError, match="1件以上"):
        HistoricalClock(TRADING_DAY, [])
    with pytest.raises(ValueError, match="同じ日付"):
        HistoricalClock(TRADING_DAY, [datetime(2026, 9, 24, 9, 0)])


def test_historical_board_returns_latest_price_at_or_before_clock():
    first = datetime(2026, 9, 25, 9, 0)
    clock = HistoricalClock(TRADING_DAY, [first, first + timedelta(minutes=1)])
    board = HistoricalBoardClient(
        {
            "1234": [
                _minute_bar("2026-09-25T09:01:00", 102.0),
                _minute_bar("2026-09-25T09:00:00", 101.0),
            ]
        },
        clock,
    )

    assert board.get_current_board("unused", "1234") == {"current_price": 101.0}
    assert board.get_current_board("unused", "missing") is None
    clock.advance(60)
    assert board.get_current_board("unused", "1234") == {"current_price": 102.0}


def test_historical_board_returns_none_before_first_bar():
    clock = HistoricalClock(TRADING_DAY, [datetime(2026, 9, 25, 9, 0)])
    board = HistoricalBoardClient(
        {"1234": [_minute_bar("2026-09-25T09:01:00", 102.0)]},
        clock,
    )

    assert board.get_current_board("unused", "1234") is None


def test_historical_market_data_excludes_simulation_day_for_all_methods():
    daily = HistoricalMarketDataClient(
        {
            "1234": [
                DatedDailyBar(date(2026, 9, 24), high=12, low=9, close=11),
                DatedDailyBar(TRADING_DAY, high=15, low=10, close=14),
            ],
            "^N225": [
                DatedDailyBar(date(2026, 9, 24), high=110, low=90, close=105, open=100),
                DatedDailyBar(TRADING_DAY, high=120, low=100, close=118, open=105),
            ],
        },
        HistoricalClock(TRADING_DAY, [datetime(2026, 9, 25, 9, 0)]),
    )

    assert daily.get_yahoo_daily_closes("1234") == [11]
    assert daily.get_yahoo_daily_bars("1234")[0].close == 11
    assert [bar.date for bar in daily.get_daily_ohlc("^N225")] == [date(2026, 9, 24)]
    assert daily.get_yahoo_daily_closes("missing") == []
    assert daily.get_daily_ohlc("1234") == []


def test_historical_filtering_repository_loads_only_clock_date(tmp_path):
    repository = HistoricalFilteringResultRepository(
        tmp_path,
        HistoricalClock(TRADING_DAY, [datetime(2026, 9, 25, 9, 0)]),
    )
    repository.save(FilteringResult(date="2026-09-24", symbols=["1111"], generated_at=""))
    repository.save(FilteringResult(date="2026-09-25", symbols=["2222"], generated_at=""))

    assert repository.load_latest().symbols == ["2222"]


def test_no_op_dependencies_return_empty_results_without_persistence():
    analyzer = NoOpDailyAnalyzer()
    repository = NoOpFilterDecisionRepository()
    observed_at = datetime(2026, 9, 25, 10, 0)

    assert analyzer.analyze({"date": "2026-09-25"}) is None
    assert repository.record_event("ATR_DANGER_SKIP", "1234", observed_at, 100.0, 100) is None
    assert repository.update_open_event_observations(observed_at, {"1234": 100.0}) == 0
    assert repository.finalize_due_events(observed_at, 1) == []
    assert repository.load_summaries() == []


def test_previous_import_paths_remain_compatible():
    assert CompatibleHistoricalClock is HistoricalClock
    assert CompatibleFilteringResultRepository is HistoricalFilteringResultRepository
