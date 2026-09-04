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


def test_check_market_data_sources_accepts_complete_market_data(monkeypatch):
    monkeypatch.setattr(run_trading, 'get_yahoo_5d_closes', lambda symbol: [100.0] * 5)
    monkeypatch.setattr(run_trading, 'get_current_board', lambda token, symbol: {'current_price': 101.0})

    assert run_trading.check_market_data_sources('dummy', ['7203', '8306'])


def test_check_market_data_sources_rejects_incomplete_market_data(monkeypatch):
    monkeypatch.setattr(run_trading, 'get_yahoo_5d_closes', lambda symbol: [100.0] * 4)
    monkeypatch.setattr(
        run_trading,
        'get_current_board',
        lambda token, symbol: (_ for _ in ()).throw(AssertionError('board should not be called')),
    )

    assert not run_trading.check_market_data_sources('dummy', ['7203'])


def test_check_market_data_sources_rejects_missing_board_price(monkeypatch):
    monkeypatch.setattr(run_trading, 'get_yahoo_5d_closes', lambda symbol: [100.0] * 5)
    monkeypatch.setattr(run_trading, 'get_current_board', lambda token, symbol: {})

    assert not run_trading.check_market_data_sources('dummy', ['7203'])
