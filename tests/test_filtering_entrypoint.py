import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.entrypoints import run_filtering
from src.entrypoints.run_filtering import RegistrationAwareBoardCache
from src.config import config


def test_main_does_not_request_token_on_non_trading_day(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['run_filtering.py'])
    monkeypatch.setattr(run_filtering, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_filtering, 'is_trading_day', lambda _: False)
    monkeypatch.setattr(
        run_filtering,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )
    monkeypatch.setattr(
        run_filtering,
        'market_workflow_lock',
        lambda: (_ for _ in ()).throw(AssertionError('market_workflow_lock was entered')),
    )

    # 例外が発生しなければ、休場日ガードで早期returnできている
    run_filtering.main()


def test_registration_aware_board_cache_deduplicates_and_clears_at_batch_limit():
    requested = []
    unregisters = []

    class BoardStub:
        def get_current_board(self, symbol):
            requested.append(symbol)
            return {"symbol": symbol}

    cache = RegistrationAwareBoardCache(
        BoardStub(),
        lambda: unregisters.append("clear") or {},
        batch_size=2,
    )

    assert cache.get_current_board("7203") == {"symbol": "7203"}
    assert cache.get_current_board("7203") == {"symbol": "7203"}
    assert cache.get_current_board("8306") == {"symbol": "8306"}
    assert cache.get_current_board("9432") == {"symbol": "9432"}

    assert requested == ["7203", "8306", "9432"]
    assert unregisters == ["clear"]
    assert cache.fetch_count == 3
    assert cache.cache_hit_count == 1


def test_main_saves_270_filter_before_isolated_price_band_failures(monkeypatch):
    events = []
    board_requests = []
    saved_results = {}
    token_requests = []
    unregister_calls = []

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    @contextmanager
    def acquired_lock():
        yield True

    class FakeBoardClient:
        def __init__(self, _token):
            pass

        def get_current_board(self, symbol):
            board_requests.append(symbol)
            return {"current_price": 100.0, "trading_value": 1000.0}

    class FakeRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def save(self, result):
            saved_results[self.directory] = result.symbols
            events.append(("saved", self.directory.name, result.symbols))

    class FakeFilteringUseCase:
        def __init__(self, _screening, board_client, _volume, result_repository, *_args, **_kwargs):
            self.board_client = board_client
            self.result_repository = result_repository

        def execute(self, target_date=None, price_cap=None, **_kwargs):
            if price_cap is None:
                self.board_client.get_current_board("common")
                self.result_repository.save(SimpleNamespace(symbols=["common"]))
                events.append(("primary_complete",))
                return SimpleNamespace(symbols=["common"])
            if price_cap == 450.0:
                self.board_client.get_current_board("common")
                self.board_client.get_current_board("450-a")
                self.board_client.get_current_board("450-b")
                events.append(("alternate_failed", price_cap))
                raise RuntimeError("injected alternative failure")
            self.board_client.get_current_board("common")
            self.board_client.get_current_board("450-a")
            self.board_client.get_current_board("450-b")
            self.board_client.get_current_board("900-a")
            self.result_repository.save(SimpleNamespace(symbols=["common", "900-a"]))
            events.append(("alternate_complete", price_cap))
            return SimpleNamespace(symbols=["common", "900-a"])

    class FakeDiagnosticsRepository:
        def __init__(self, _directory):
            pass

    monkeypatch.setattr(sys, "argv", ["run_filtering.py"])
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", acquired_lock)
    monkeypatch.setattr(run_filtering, "process_notification", context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: token_requests.append("token") or "token")
    monkeypatch.setattr(run_filtering, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(
        run_filtering,
        "unregister_all",
        lambda _token: unregister_calls.append("clear") or events.append(("unregister",)) or {},
    )
    monkeypatch.setattr(run_filtering, "BoardClient", FakeBoardClient)
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(run_filtering, "ScreeningResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", FakeDiagnosticsRepository)
    monkeypatch.setattr(run_filtering, "DecisionJournalRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "SCREENING_BATCH_SIZE", 2)

    run_filtering.main()

    assert saved_results[config.FILTERING_RESULT_DIRECTORY] == ["common"]
    assert saved_results[config.FILTERING_PRICE_BAND_RESULT_ROOT / "900"] == ["common", "900-a"]
    assert board_requests == ["common", "450-a", "450-b", "900-a"]
    assert events.index(("primary_complete",)) < events.index(("alternate_failed", 450.0))
    assert events.index(("alternate_failed", 450.0)) < events.index(("alternate_complete", 900.0))
    assert token_requests == ["token"]
    assert len(unregister_calls) == 4


def test_price_band_deadline_preserves_primary_and_releases_lock_after_unregister(monkeypatch, tmp_path):
    from src.application.filtering_usecase import FilteringDeadlineExceeded
    from src.infrastructure.execution_lock import market_workflow_lock as actual_market_workflow_lock
    from src.infrastructure import execution_lock

    events = []
    saved_results = {}
    saved_diagnostics = []
    unregister_calls = []
    original_datetime = run_filtering.datetime

    class FixedDateTime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 5, 9, 34)

    @contextmanager
    def process_context(*_args, **_kwargs):
        yield

    @contextmanager
    def tracked_lock():
        with actual_market_workflow_lock() as acquired:
            yield acquired
        events.append(("lock_released",))

    class FakeRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def save(self, result):
            saved_results[self.directory] = result.symbols
            events.append(("result_saved", self.directory))

    class FakeDiagnosticsRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

        def save(self, diagnostic):
            saved_diagnostics.append((self.directory, diagnostic))
            events.append(("diagnostics_saved", self.directory))

    class FakeFilteringUseCase:
        def __init__(self, _screening, _board, _volume, result_repository, *_args, **kwargs):
            self.result_repository = result_repository
            self.diagnostics_repository = kwargs.get("diagnostics_repository")

        def execute(self, target_date=None, price_cap=None, **kwargs):
            if price_cap is None:
                self.result_repository.save(SimpleNamespace(symbols=["primary-270"]))
                self.diagnostics_repository.save({"baseline": True})
                return SimpleNamespace(symbols=["primary-270"])
            assert kwargs["deadline_monotonic"] == 100.0
            diagnostic = {
                "price_band": kwargs["price_band"],
                "result_saved": False,
                "summary": {"timed_out": True, "stop_reason": "FILTER_TIME_LIMIT"},
                "candidates": [],
            }
            self.diagnostics_repository.save(diagnostic)
            raise FilteringDeadlineExceeded("injected deadline")

    monkeypatch.setattr(execution_lock, "LOCK_FILE", tmp_path / ".market_workflow.lock")
    monkeypatch.setattr(sys, "argv", ["run_filtering.py"])
    monkeypatch.setattr(run_filtering, "datetime", FixedDateTime)
    monkeypatch.setattr(run_filtering, "monotonic", lambda: 100.0)
    monkeypatch.setattr(run_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_filtering, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_filtering, "market_workflow_lock", tracked_lock)
    monkeypatch.setattr(run_filtering, "process_notification", process_context)
    monkeypatch.setattr(run_filtering, "get_api_token", lambda: "token")
    monkeypatch.setattr(run_filtering, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(
        run_filtering,
        "unregister_all",
        lambda _token: unregister_calls.append("clear") or events.append(("unregistered",)) or {},
    )
    monkeypatch.setattr(run_filtering, "FilteringUseCase", FakeFilteringUseCase)
    monkeypatch.setattr(run_filtering, "ScreeningResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringResultRepository", FakeRepository)
    monkeypatch.setattr(run_filtering, "FilteringDiagnosticsRepository", FakeDiagnosticsRepository)
    monkeypatch.setattr(run_filtering, "DecisionJournalRepository", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(run_filtering, "YahooFinanceClient", lambda: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))
    monkeypatch.setattr(config, "FILTERING_PRICE_BAND_DEADLINE_TIME", datetime.strptime("09:33", "%H:%M").time())

    run_filtering.main()

    assert saved_results[config.FILTERING_RESULT_DIRECTORY] == ["primary-270"]
    assert len(saved_diagnostics) == 3
    assert [item[1]["price_band"] for item in saved_diagnostics[1:]] == ["450", "900"]
    assert all(item[1]["result_saved"] is False for item in saved_diagnostics[1:])
    assert events.index(("unregistered",)) < events.index(("lock_released",))
    assert len(unregister_calls) == 3
    with actual_market_workflow_lock() as acquired:
        assert acquired is True
