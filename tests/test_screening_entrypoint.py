import sys

from src.entrypoints import run_screening


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
