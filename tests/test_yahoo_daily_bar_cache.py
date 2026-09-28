import json
from datetime import date

from src.domain.volatility import DailyBar
from src.infrastructure.market_data import yahoo_backtest_history_client
from src.infrastructure.market_data import yahoo_daily_bar_cache
from src.infrastructure.market_data.yahoo_backtest_history_client import calculate_required_fetch_days


def test_calculate_required_fetch_days_anchors_range_to_earliest_simulation_date():
    assert calculate_required_fetch_days(
        date(2026, 9, 7),
        today=date(2026, 9, 28),
    ) == 81
    assert calculate_required_fetch_days(
        date(2026, 10, 1),
        today=date(2026, 9, 28),
    ) == 60


def test_cached_fetch_saves_ohlc_and_reuses_sufficient_cache(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(symbols, days):
        calls.append((symbols, days))
        return {
            symbol: {
                "2026-09-24": DailyBar(open=99.0, high=102.0, low=98.0, close=101.0),
                "2026-09-25": DailyBar(open=101.0, high=103.0, low=100.0, close=102.0),
            }
            for symbol in symbols
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    first = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133", "^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [(["3133", "^N225"], 81)]
    assert first["^N225"]["2026-09-25"].open == 101.0
    saved = json.loads((tmp_path / "^N225.json").read_text(encoding="utf-8"))
    assert saved["2026-09-25"] == {
        "open": 101.0,
        "high": 103.0,
        "low": 100.0,
        "close": 102.0,
    }

    second = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133", "^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )
    assert calls == [(["3133", "^N225"], 81)]
    assert second["3133"]["2026-09-24"].close == 101.0


def test_cached_fetch_refreshes_when_latest_required_date_is_not_cached(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "3133",
        {"2026-09-24": DailyBar(open=99.0, high=102.0, low=98.0, close=101.0)},
    )
    calls = []

    def fake_fetch(symbols, days):
        calls.append((symbols, days))
        return {
            "3133": {
                "2026-09-25": DailyBar(open=101.0, high=103.0, low=100.0, close=102.0),
            }
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["3133"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 24),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [(["3133"], 81)]
    assert sorted(history["3133"]) == ["2026-09-24", "2026-09-25"]


def test_dated_ohlc_fetcher_preserves_open_for_index_market_regime(monkeypatch):
    timestamp = 1790294400

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "chart": {
                    "result": [{
                        "timestamp": [timestamp],
                        "indicators": {"quote": [{
                            "open": [100.0],
                            "high": [110.0],
                            "low": [95.0],
                            "close": [105.0],
                        }]},
                    }]
                }
            }

    monkeypatch.setattr(yahoo_backtest_history_client.requests, "get", lambda *args, **kwargs: Response())
    history = yahoo_backtest_history_client.fetch_yahoo_dated_ohlc(["^N225"], days=81)

    assert list(history["^N225"].values())[0].open == 100.0


def test_index_cache_without_open_is_refetched(tmp_path, monkeypatch):
    yahoo_daily_bar_cache._save_cache(
        tmp_path,
        "^N225",
        {"2026-09-25": DailyBar(high=110.0, low=95.0, close=105.0)},
    )
    calls = []

    def fake_fetch(symbols, days):
        calls.append(symbols)
        return {
            "^N225": {
                "2026-09-25": DailyBar(open=100.0, high=110.0, low=95.0, close=105.0),
            }
        }

    monkeypatch.setattr(yahoo_daily_bar_cache, "fetch_yahoo_dated_ohlc", fake_fetch)
    history = yahoo_daily_bar_cache.fetch_yahoo_dated_ohlc_cached(
        ["^N225"],
        days=81,
        cache_dir=tmp_path,
        earliest_needed_date=date(2026, 9, 25),
        latest_needed_date=date(2026, 9, 25),
    )

    assert calls == [["^N225"]]
    assert history["^N225"]["2026-09-25"].open == 100.0