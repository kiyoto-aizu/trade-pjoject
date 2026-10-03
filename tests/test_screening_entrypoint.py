import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.entrypoints import run_screening
from src.config import config


def test_main_does_not_request_token_on_non_trading_day(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['run_screening.py'])
    monkeypatch.setattr(run_screening, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_screening, 'is_trading_day', lambda _: False)
    monkeypatch.setattr(
        run_screening,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )
    monkeypatch.setattr(
        run_screening,
        'market_workflow_lock',
        lambda: (_ for _ in ()).throw(AssertionError('market_workflow_lock was entered')),
    )

    # 例外が発生しなければ、休場日ガードで早期returnできている
    run_screening.main()


def test_main_saves_primary_screening_before_price_band_results(monkeypatch):
    events = []
    usecases = []
    token_requests = []

    @contextmanager
    def context(*_args, **_kwargs):
        yield

    @contextmanager
    def acquired_lock():
        yield True

    class FakeScreeningUseCase:
        def __init__(self, *args):
            self.args = args
            self.batch_started = None
            self.batch_finished = None
            usecases.append(self)

        def execute(self, target_date=None, price_cap=None, keep_unconfirmed=False):
            events.append(("execute", price_cap, keep_unconfirmed))
            if price_cap is None:
                self.batch_started(["7203"], 1)
                self.batch_finished(["7203"], 1)
            return SimpleNamespace(symbols=["7203"])

    class FakeCheckRepository:
        def __init__(self, directory, target_date, exchange_repository, regulation_repository):
            self.directory = directory
            self.exchange_repository = exchange_repository
            self.regulation_repository = regulation_repository

    class FakeResultRepository:
        def __init__(self, directory):
            self.directory = Path(directory)

    monkeypatch.setattr(sys, "argv", ["run_screening.py"])
    monkeypatch.setattr(run_screening, "configure_logging", lambda: None)
    monkeypatch.setattr(run_screening, "is_trading_day", lambda _day: True)
    monkeypatch.setattr(run_screening, "market_workflow_lock", acquired_lock)
    monkeypatch.setattr(run_screening, "process_notification", context)
    monkeypatch.setattr(run_screening, "get_api_token", lambda: token_requests.append("token") or "token")
    monkeypatch.setattr(run_screening, "get_token_provider", lambda: SimpleNamespace(recovery_failed=False))
    monkeypatch.setattr(run_screening, "unregister_all", lambda _token: events.append(("unregister",)) or {})
    monkeypatch.setattr(run_screening, "register_symbols", lambda _token, symbols: events.append(("register", len(symbols))) or {})
    monkeypatch.setattr(run_screening, "ScreeningUseCase", FakeScreeningUseCase)
    monkeypatch.setattr(run_screening, "ScreeningApiCheckRepository", FakeCheckRepository)
    monkeypatch.setattr(run_screening, "ScreeningResultRepository", FakeResultRepository)
    monkeypatch.setattr(run_screening, "HistoricalRankingRepository", lambda *_args: object())
    monkeypatch.setattr(run_screening, "ListedSecurityRepository", lambda *_args: object())
    monkeypatch.setattr(run_screening, "RegulationRepository", lambda _token: object())
    monkeypatch.setattr(run_screening, "PrimaryExchangeRepository", lambda _token: object())
    monkeypatch.setattr(config, "SCREENING_ALTERNATE_PRICE_CAPS", (450.0, 900.0))

    run_screening.main()

    assert [event for event in events if event[0] == "execute"] == [
        ("execute", None, False),
        ("execute", 450.0, True),
        ("execute", 900.0, True),
    ]
    assert events.index(("execute", None, False)) < events.index(("execute", 450.0, True))
    assert token_requests == ["token"]
    assert len(usecases) == 3
    assert usecases[0].args[1] is usecases[0].args[2]
    assert all(usecase.args[1] is usecases[0].args[1] for usecase in usecases)
    assert [usecase.args[3].directory for usecase in usecases] == [
        config.SCREENING_RESULT_DIRECTORY,
        config.SCREENING_PRICE_BAND_RESULT_ROOT / "450",
        config.SCREENING_PRICE_BAND_RESULT_ROOT / "900",
    ]
