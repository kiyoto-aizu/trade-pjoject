import sys

from src.entrypoints import run_filtering


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
