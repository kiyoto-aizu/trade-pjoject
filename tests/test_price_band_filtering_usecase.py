import logging
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.filtering_usecase import BoardRetryPolicy, FilteringDeadlineExceeded
from src.application.price_band_filtering_usecase import PriceBandFilteringUseCase
from src.config import config


def _build_use_case(monkeypatch, caplog, outcomes, unregister_results=(True, True), counter_changes=None):
    events = []
    executions = []
    clear_results = iter(unregister_results)
    board_cache = SimpleNamespace(
        fetch_count=10,
        cache_hit_count=20,
        fetch_duration_ms=30.0,
        unregister_count=6,
        max_concurrency_observed=3,
    )

    def get_metrics_snapshot():
        return {
            "fetch_count": board_cache.fetch_count,
            "cache_hit_count": board_cache.cache_hit_count,
            "fetch_duration_ms": board_cache.fetch_duration_ms,
            "max_concurrency_observed": board_cache.max_concurrency_observed,
            "rate_limit": {"responses": 0, "retries": 0, "succeeded": 0, "failed": 0},
        }

    def fetch_current_boards(symbols, max_workers, should_start=None):
        unique = list(dict.fromkeys(symbols))
        events.append(("prefetch", unique, max_workers))
        board_cache.fetch_count += len(unique)
        return {symbol: {"symbol": symbol} for symbol in unique}

    board_cache.get_metrics_snapshot = get_metrics_snapshot
    board_cache.fetch_current_boards = fetch_current_boards

    def clear_registrations():
        events.append("clear")
        board_cache.unregister_count += 1
        return next(clear_results)

    board_cache.clear_registrations = clear_registrations

    class FakeScreeningRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def load_for_date(self, _day):
            if self.directory.name == "450":
                return SimpleNamespace(symbols=["common", "450-a", "450-b"])
            if self.directory.name == "900":
                return SimpleNamespace(symbols=["common", "900-a"])
            return SimpleNamespace(symbols=["common"])

    class FakePrimaryUseCase:
        screening_repository = FakeScreeningRepository(Path("primary"))

        def execute(self, **kwargs):
            executions.append(kwargs)
            events.append(("execute", None))
            return SimpleNamespace(symbols=[])

    def filtering_factory(_screening, _board, _volume, _result, **_kwargs):
        class FakeFilteringUseCase:
            def execute(self, **kwargs):
                price_cap = kwargs["price_cap"]
                executions.append(kwargs)
                events.append(("execute", price_cap))
                changes = (counter_changes or {}).get(price_cap, (0, 0, 0.0))
                board_cache.fetch_count += changes[0]
                board_cache.cache_hit_count += changes[1]
                board_cache.fetch_duration_ms += changes[2]
                outcome = outcomes.get(price_cap)
                if isinstance(outcome, BaseException):
                    raise outcome
                return SimpleNamespace(symbols=outcome)

        return FakeFilteringUseCase()

    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_DEADLINE_TIME", time(9, 33))
    caplog.set_level(logging.INFO)
    clock_value = [0.0]

    def perf_counter():
        clock_value[0] += 1.0
        return clock_value[0]

    use_case = PriceBandFilteringUseCase(
        board_cache,
        object(),
        now=lambda: datetime(2026, 10, 5, 9, 30),
        monotonic_clock=lambda: 100.0,
        perf_counter_clock=perf_counter,
        filtering_use_case_factory=filtering_factory,
        screening_repository_factory=FakeScreeningRepository,
        filtering_repository_factory=lambda directory: Path(directory),
        diagnostics_repository_factory=lambda directory: Path(directory),
        logger=logging.getLogger("test.price_band_filtering"),
    )
    return use_case, board_cache, executions, events, FakePrimaryUseCase()


def test_runs_all_price_bands_and_preserves_summary_counters(monkeypatch, caplog):
    use_case, board_cache, executions, events, primary_usecase = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: ["a"], 900.0: ["a", "b"]},
        counter_changes={450.0: (1, 1, 0.5), 900.0: (2, 3, 2.0)},
    )

    retry_policy = BoardRetryPolicy(2, 10.0, 240.0, datetime(2026, 10, 5, 9, 32, 30))
    use_case.run(primary_usecase, board_retry=retry_policy)

    assert [item.get("price_cap") for item in executions] == [None, 450.0, 900.0]
    assert all(item.get("deadline_monotonic") == 280.0 for item in executions[1:])
    assert all(item.get("board_retry") is retry_policy for item in executions)
    assert events == [
        ("prefetch", ["common"], config.FILTER_BOARD_MAX_CONCURRENCY),
        ("execute", None),
        "clear",
        ("prefetch", ["450-a", "450-b"], config.FILTER_BOARD_MAX_CONCURRENCY),
        ("execute", 450.0),
        ("prefetch", ["900-a"], config.FILTER_BOARD_MAX_CONCURRENCY),
        ("execute", 900.0),
        "clear",
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("価格帯別フィルタ完了: 上限=450円") for message in messages)
    assert any(message.startswith("価格帯別フィルタ完了: 上限=900円") for message in messages)
    summary = next(record for record in caplog.records if record.msg.startswith("全価格帯板取得サマリー:"))
    assert summary.args == (7, 4, 2.5, 11000.0, 7)
    assert board_cache.unregister_count == 8


def test_unregister_failure_skips_all_price_bands_and_logs_error(monkeypatch, caplog):
    use_case, _board_cache, executions, events, primary_usecase = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: ["a"], 900.0: ["b"]},
        unregister_results=(False, False),
    )

    use_case.run(primary_usecase)

    assert [item.get("price_cap") for item in executions] == [None]
    assert events == [("prefetch", ["common"], config.FILTER_BOARD_MAX_CONCURRENCY), ("execute", None), "clear", "clear"]
    assert [record.getMessage() for record in caplog.records].count(
        "通常フィルタ後の登録解除に失敗したため、追加価格帯フィルタを中止します。"
    ) == 1
    assert "フィルタ終了時の銘柄登録解除に失敗しました。" in [
        record.getMessage() for record in caplog.records
    ]


def test_deadline_warning_does_not_prevent_next_price_band(monkeypatch, caplog):
    use_case, _board_cache, executions, _events, primary_usecase = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: FilteringDeadlineExceeded("時間切れ"), 900.0: ["b"]},
    )

    use_case.run(primary_usecase)

    assert [item.get("price_cap") for item in executions] == [None, 450.0, 900.0]
    assert any(
        record.getMessage().startswith("価格帯別フィルタを締め切りで打ち切りました: 上限=450円")
        and record.getMessage().endswith("時間切れ")
        for record in caplog.records
    )


def test_price_band_exception_is_logged_and_next_band_runs(monkeypatch, caplog):
    use_case, _board_cache, executions, _events, primary_usecase = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: RuntimeError("injected failure"), 900.0: ["b"]},
    )

    use_case.run(primary_usecase)

    assert [item.get("price_cap") for item in executions] == [None, 450.0, 900.0]
    assert "価格帯別フィルタに失敗しました: 上限=450円" in [record.getMessage() for record in caplog.records]