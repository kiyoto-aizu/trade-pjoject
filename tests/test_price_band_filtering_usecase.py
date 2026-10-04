import logging
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.filtering_usecase import FilteringDeadlineExceeded
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
    )

    def clear_registrations():
        events.append("clear")
        board_cache.unregister_count += 1
        return next(clear_results)

    board_cache.clear_registrations = clear_registrations

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
    clocks = iter((5.0, 8.0))
    use_case = PriceBandFilteringUseCase(
        board_cache,
        object(),
        now=lambda: datetime(2026, 10, 5, 9, 30),
        monotonic_clock=lambda: 100.0,
        perf_counter_clock=lambda: next(clocks),
        filtering_use_case_factory=filtering_factory,
        screening_repository_factory=lambda directory: Path(directory),
        filtering_repository_factory=lambda directory: Path(directory),
        diagnostics_repository_factory=lambda directory: Path(directory),
        logger=logging.getLogger("test.price_band_filtering"),
    )
    return use_case, board_cache, executions, events


def test_runs_all_price_bands_and_preserves_summary_counters(monkeypatch, caplog):
    use_case, board_cache, executions, events = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: ["a"], 900.0: ["a", "b"]},
        counter_changes={450.0: (1, 1, 0.5), 900.0: (2, 3, 2.0)},
    )

    use_case.run()

    assert [item["price_cap"] for item in executions] == [450.0, 900.0]
    assert all(item["deadline_monotonic"] == 280.0 for item in executions)
    assert events == ["clear", ("execute", 450.0), ("execute", 900.0), "clear"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("価格帯別フィルタ完了: 上限=450円") for message in messages)
    assert any(message.startswith("価格帯別フィルタ完了: 上限=900円") for message in messages)
    summary = next(record for record in caplog.records if record.msg.startswith("価格帯別板取得サマリー:"))
    assert summary.args == (3, 4, 2.5, 3000.0, 7)
    assert board_cache.unregister_count == 8


def test_unregister_failure_skips_all_price_bands_and_logs_error(monkeypatch, caplog):
    use_case, _board_cache, executions, events = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: ["a"], 900.0: ["b"]},
        unregister_results=(False, False),
    )

    use_case.run()

    assert executions == []
    assert events == ["clear", "clear"]
    assert [record.getMessage() for record in caplog.records].count(
        "270円フィルタ保存後の銘柄登録解除に失敗したため、追加価格帯フィルタを中止します。"
    ) == 1
    assert "フィルタ終了時の銘柄登録解除に失敗しました。" in [
        record.getMessage() for record in caplog.records
    ]


def test_deadline_warning_does_not_prevent_next_price_band(monkeypatch, caplog):
    use_case, _board_cache, executions, _events = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: FilteringDeadlineExceeded("時間切れ"), 900.0: ["b"]},
    )

    use_case.run()

    assert [item["price_cap"] for item in executions] == [450.0, 900.0]
    assert "価格帯別フィルタを締め切りで打ち切りました: 上限=450円 | 時間切れ" in [
        record.getMessage() for record in caplog.records
    ]


def test_price_band_exception_is_logged_and_next_band_runs(monkeypatch, caplog):
    use_case, _board_cache, executions, _events = _build_use_case(
        monkeypatch,
        caplog,
        {450.0: RuntimeError("injected failure"), 900.0: ["b"]},
    )

    use_case.run()

    assert [item["price_cap"] for item in executions] == [450.0, 900.0]
    assert "価格帯別フィルタに失敗しました: 上限=450円" in [
        record.getMessage() for record in caplog.records
    ]