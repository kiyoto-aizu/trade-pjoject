from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.paper.paper_order_executor import PaperOrderExecutor


def test_paper_order_executor_updates_virtual_cash_holdings_and_order_log():
    executor = PaperOrderExecutor(prices={'7203': 90.0}, cash=10_000.0, order_qty=100)

    buy_result = executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    sell_result = executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert buy_result['Result'] == 0
    assert sell_result['OrderId'] == 'paper-2'
    assert executor.cash == 10_000.0
    assert executor.holdings == {}
    assert len(executor.orders) == 2


def test_paper_order_executor_rejects_orders_without_changing_state():
    executor = PaperOrderExecutor(prices={'7203': 90.0}, cash=1_000.0, order_qty=100)

    buy_result = executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    sell_result = executor.place_market_order('unused', '7203', config.OrderSide.SELL.value)

    assert buy_result is None
    assert sell_result is None
    assert executor.cash == 1_000.0
    assert executor.holdings == {}
    assert executor.orders == []


def test_paper_order_executor_restores_account_state_after_restart(tmp_path):
    state_path = tmp_path / 'paper_account_state.json'
    executor = PaperOrderExecutor(
        prices={'7203': 90.0},
        cash=10_000.0,
        order_qty=100,
        state_path=state_path,
    )
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)

    restarted = PaperOrderExecutor(
        prices={'7203': 90.0},
        cash=1.0,
        order_qty=100,
        state_path=state_path,
    )

    assert restarted.cash == 1_000.0
    assert restarted.holdings == {'7203': 100}
    assert restarted.get_wallet_cash('unused') == {'StockAccountWallet': 1_000.0}
    assert restarted.get_positions('unused')[0]['HoldQty'] == 100


def test_trading_use_case_uses_paper_account_instead_of_live_account_clients(monkeypatch, tmp_path):
    executor = PaperOrderExecutor(prices={'7203': 90.0}, cash=10_000.0, order_qty=100)
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)
    use_case = TradingUseCase(
        token='unused',
        order_history_path=tmp_path / 'order_history.json',
        order_sender=executor,
    )

    monkeypatch.setattr(
        'src.application.trading_usecase.get_wallet_cash',
        lambda token: (_ for _ in ()).throw(AssertionError('live wallet API was called')),
    )
    monkeypatch.setattr(
        'src.application.trading_usecase.get_positions',
        lambda token: (_ for _ in ()).throw(AssertionError('live positions API was called')),
    )

    wallet, positions = use_case._load_account_state()

    assert wallet == 1_000.0
    assert positions[0]['Symbol'] == '7203'
