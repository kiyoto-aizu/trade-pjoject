from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
import logging

from src.application.filtering_usecase import FilteringUseCase
from src.application.price_band_filtering_usecase import PriceBandFilteringUseCase
from src.config import config
from src.infrastructure.kabu.registration_aware_board_cache import RegistrationAwareBoardCache


class _Board:
    def get_current_board(self, symbol):
        return {"symbol": symbol, "trading_value": 1.0}


def test_cache_records_one_log_entry_per_fetch_with_band_and_registration_count():
    cache = RegistrationAwareBoardCache(_Board(), lambda: True, batch_size=2, max_workers=1)
    cache.current_band = "270"
    cache.fetch_current_boards(["A", "B", "C"])
    cache.current_band = "450"
    cache.fetch_current_boards(["C", "D"])

    log = cache.get_fetch_log()
    assert [(e["symbol"], e["band"]) for e in log] == [("A", "270"), ("B", "270"), ("C", "270"), ("D", "450")]
    assert [e["registered_count"] for e in log] == [1, 2, 1, 2]
    assert all(e["kind"] == "initial" and e["ok"] and e["elapsed_ms"] >= 0 for e in log)
    assert all(e["started_at"] <= e["completed_at"] for e in log)
    assert [e["symbol"] for e in cache.get_fetch_log("450")] == ["D"]


def test_fetch_stats_total_median_max():
    stats = FilteringUseCase._board_fetch_stats([
        {"kind": "initial", "elapsed_ms": 3000.0},
        {"kind": "initial", "elapsed_ms": 1000.0},
        {"kind": "initial", "elapsed_ms": 8000.0},
        {"kind": "retry", "elapsed_ms": 500.0},
    ])
    assert stats["fetch_count"] == 3
    assert stats["total_ms"] == 12000.0
    assert stats["median_ms"] == 3000.0
    assert stats["max_ms"] == 8000.0
    assert stats["retry_fetch_count"] == 1


def test_late_band_prefetch_not_started_after_deadline_and_earlier_band_runs_first(monkeypatch):
    events = []
    clock = [0.0]

    def fetch_current_boards(symbols, max_workers, should_start=None):
        started = []
        for symbol in symbols:
            if should_start is not None and not should_start():
                continue
            started.append(symbol)
            clock[0] += 100.0
        events.append(("prefetch", list(symbols), started))
        return {symbol: {} for symbol in started}

    cache = SimpleNamespace(
        fetch_count=0, cache_hit_count=0, fetch_duration_ms=0.0, unregister_count=0, current_band=None,
        fetch_current_boards=fetch_current_boards, clear_registrations=lambda: True,
    )

    class Repo:
        def __init__(self, directory):
            self.directory = Path(directory)

        def load_for_date(self, _day):
            names = {"450": ["x", "450-a", "450-b"], "900": ["900-a"]}
            return SimpleNamespace(symbols=names.get(self.directory.name, ["x"]))

    def factory(*_args, **_kwargs):
        return SimpleNamespace(execute=lambda **kw: events.append(("execute", kw["price_cap"], kw["board_prefetch_summary"]))
                               or SimpleNamespace(symbols=[]))

    primary = SimpleNamespace(
        screening_repository=Repo(Path("primary")),
        execute=lambda **kw: events.append(("execute", None, kw["board_prefetch_summary"])),
    )
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", True)
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_DEADLINE_TIME", time(9, 34))
    use_case = PriceBandFilteringUseCase(
        cache, object(),
        now=lambda: datetime(2026, 10, 5, 9, 32, 30),
        monotonic_clock=lambda: clock[0],
        filtering_use_case_factory=factory,
        screening_repository_factory=Repo,
        filtering_repository_factory=lambda d: Path(d),
        diagnostics_repository_factory=lambda d: Path(d),
        logger=logging.getLogger("test.band_order"),
    )
    use_case.run(primary)

    # 主帯が先に完結し、重複銘柄xは450帯で再取得されない。deadline=90秒後、主帯の取得で100秒経過済みのため後続帯は未着手
    assert events[0] == ("prefetch", ["x"], ["x"])
    assert events[1][0:2] == ("execute", None)
    assert events[2] == ("prefetch", ["450-a", "450-b"], [])
    assert events[3][0:2] == ("execute", 450.0)
    assert events[3][2]["not_started_count"] == 2
    assert events[3][2]["reused_from_earlier_band_count"] == 1
    assert events[3][2]["band"] == "450"
    assert events[5][2]["not_started_count"] == 1
