from datetime import datetime

from src.entrypoints import run_trading


def test_is_trading_session_accepts_weekday_market_hours():
    assert run_trading.is_trading_session(datetime(2026, 9, 4, 9, 0))
    assert run_trading.is_trading_session(datetime(2026, 9, 4, 15, 29))


def test_is_trading_session_rejects_boundaries_and_weekends():
    assert not run_trading.is_trading_session(datetime(2026, 9, 4, 8, 59))
    assert not run_trading.is_trading_session(datetime(2026, 9, 4, 15, 30))
    assert not run_trading.is_trading_session(datetime(2026, 9, 5, 10, 0))


def test_main_does_not_request_token_outside_trading_session(monkeypatch):
    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(
        run_trading,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )

    run_trading.main(now_provider=lambda: datetime(2026, 9, 5, 10, 0))
