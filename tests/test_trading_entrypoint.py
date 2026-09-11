from datetime import datetime

from src.config import config
from src.entrypoints import run_trading
from src.domain.rules import is_trading_session


def test_is_trading_session_accepts_weekday_market_hours():
    assert is_trading_session(datetime(2026, 9, 4, 9, 0), 9, 0, 15, 30)
    assert is_trading_session(datetime(2026, 9, 4, 15, 29), 9, 0, 15, 30)


def test_is_trading_session_rejects_boundaries_and_weekends():
    assert not is_trading_session(datetime(2026, 9, 4, 8, 59), 9, 0, 15, 30)
    assert not is_trading_session(datetime(2026, 9, 4, 15, 30), 9, 0, 15, 30)
    assert not is_trading_session(datetime(2026, 9, 5, 10, 0), 9, 0, 15, 30)


def test_main_does_not_request_token_outside_trading_session(monkeypatch):
    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(
        run_trading,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )

    run_trading.main(now_provider=lambda: datetime(2026, 9, 5, 10, 0))


def test_main_can_enter_the_pre_close_liquidation_path(monkeypatch):
    class FilteringResult:
        symbols = ['7203']

    class FilteringRepository:
        def __init__(self, path):
            pass

        def load_for_date(self, target_date):
            return FilteringResult()

    class Bot:
        def __init__(self, token):
            assert token == 'dummy'
            self.was_run = False

        def run(self, **kwargs):
            self.was_run = True

    class NoOpContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_trading, 'FilteringResultRepository', FilteringRepository)
    monkeypatch.setattr(run_trading, 'create_trading_use_case', Bot)
    monkeypatch.setattr(run_trading, 'get_api_token', lambda: 'dummy')
    monkeypatch.setattr(run_trading, 'market_workflow_lock', NoOpContext)
    monkeypatch.setattr(run_trading, 'process_notification', lambda *args, **kwargs: NoOpContext())
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', False)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)

    run_trading.main(now_provider=lambda: datetime(2026, 9, 4, 15, 20))
