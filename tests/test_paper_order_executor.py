from src.config import config
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
