import json
from datetime import datetime, time
from pathlib import Path

import pytest

from src.config import config
from src.domain.models import PriceLimit, TradeSignal
from src.domain.rules import is_market_closed
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.paper.paper_order_executor import PaperOrderExecutor
from src.trading.trading import TradingBot


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv('API_PASSWORD_DEV', 'dummy')
    monkeypatch.setenv('IS_DEMO', 'true')
    return monkeypatch


def test_required_env_helper_raises_when_missing():
    with pytest.raises(ValueError, match='Missing required environment variable: MISSING_ENV_FOR_TEST'):
        from src.config import config
        config._load_required_env('MISSING_ENV_FOR_TEST', allow_missing=False)


def test_is_market_closed_boundary():
    assert not is_market_closed(time(15, 29), 15, 30)
    assert is_market_closed(time(15, 30), 15, 30)
    assert is_market_closed(time(16, 0), 15, 30)


def test_order_history_load_corrupt(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text('not-json', encoding='utf-8')

    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    with pytest.raises(ValueError):
        use_case._load_order_history()


def test_order_history_register_records_audit_fields(tmp_path):
    history_file = tmp_path / 'order_history.json'
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    use_case._load_order_history()

    signal = TradeSignal(symbol='1475', side=config.OrderSide.BUY, price=1000.0, qty=100)
    limit = PriceLimit(buy=990.0, sell=1010.0)
    use_case._register_order(signal, limit, {'Result': 0, 'OrderId': 'abc123'})

    assert len(use_case.order_history) == 1
    entry = use_case.order_history[0]
    assert entry.symbol == '1475'
    assert entry.result_code == 0
    assert entry.order_id == 'abc123'
    assert entry.basis_buy_limit == 990.0
    assert entry.basis_sell_limit == 1010.0

    # 再読み込みしても永続化されていること
    reloaded = TradingUseCase(token='dummy', order_history_path=history_file)
    reloaded._load_order_history()
    assert len(reloaded.order_history) == 1
    assert reloaded.order_history[0].order_id == 'abc123'


def test_has_holdings_checks_sell_side_positions():
    use_case = TradingUseCase(token='dummy', order_history_path=Path('unused_history.json'))
    positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': '1', 'HoldQty': '0'},
    ]

    assert use_case._has_holdings('1475', positions)
    assert not use_case._has_holdings('7203', positions)
    assert not use_case._has_holdings('9999', positions)


def test_trading_bot_uses_paper_executor_by_default(monkeypatch):
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ENABLE_LIVE_ORDERING', False)

    bot = TradingBot(token='dummy')

    assert isinstance(bot.order_sender, PaperOrderExecutor)


def test_trading_use_case_places_and_records_buy_order_without_live_api(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    calls = []

    class MarketDataClient:
        def get_yahoo_5d_closes(self, symbol):
            return [100.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 90.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            calls.append((token, symbol, side))
            return {'Result': 0, 'OrderId': 'paper-order-1'}

    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    monkeypatch.setattr('src.application.trading_usecase.get_api_soft_limit', lambda token: 100_000.0)

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        order_sender=OrderSender(),
        notifier=lambda message: None,
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert calls == [('dummy', '7203', config.OrderSide.BUY.value)]
    assert len(use_case.order_history) == 1
    assert use_case.order_history[0].result_code == 0
    assert use_case.order_history[0].order_id == 'paper-order-1'


def test_trading_use_case_does_not_record_rejected_order(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    order_calls = []
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])

    monkeypatch.setattr('src.application.trading_usecase.get_yahoo_5d_closes', lambda symbol: [100.0] * 5)
    monkeypatch.setattr(
        'src.application.trading_usecase.get_current_board',
        lambda token, symbol: {'current_price': 90.0},
    )
    monkeypatch.setattr(
        'src.application.trading_usecase.get_wallet_cash',
        lambda token: {'StockAccountWallet': 100_000.0},
    )
    monkeypatch.setattr('src.application.trading_usecase.get_positions', lambda token: [])
    monkeypatch.setattr('src.application.trading_usecase.get_api_soft_limit', lambda token: 100_000.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.place_market_order',
        lambda token, symbol, side: order_calls.append((symbol, side)) or {'Result': 1, 'OrderId': 'rejected'},
    )

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        notifier=lambda message: None,
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert order_calls == [('7203', config.OrderSide.BUY.value)]
    assert use_case.order_history == []
