"""実注文を発生させないペーパートレード用の注文実行実装。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.config import config


@dataclass
class PaperOrderExecutor:
    """現在価格を使って仮想残高と保有株を更新する注文実行器。"""

    prices: Dict[str, float]
    cash: float = 1_000_000.0
    order_qty: int = field(default_factory=lambda: config.DEFAULT_ORDER_QTY)
    holdings: Dict[str, int] = field(default_factory=dict)
    orders: List[dict] = field(default_factory=list)
    _next_order_id: int = 1

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def place_market_order(self, token: str, symbol: str, side: str) -> Optional[dict]:
        """成行注文を現在価格で仮想約定し、実注文と同じ形式の結果を返す。"""
        del token
        price = self.prices.get(symbol)
        if price is None:
            return None

        quantity = self.order_qty
        held_quantity = self.holdings.get(symbol, 0)
        if side == config.OrderSide.BUY.value:
            required_cash = price * quantity
            if self.cash < required_cash:
                return None
            self.cash -= required_cash
            self.holdings[symbol] = held_quantity + quantity
        elif side == config.OrderSide.SELL.value:
            if held_quantity < quantity:
                return None
            self.cash += price * quantity
            remaining_quantity = held_quantity - quantity
            if remaining_quantity:
                self.holdings[symbol] = remaining_quantity
            else:
                self.holdings.pop(symbol, None)
        else:
            return None

        order_id = f"paper-{self._next_order_id}"
        self._next_order_id += 1
        order = {
            "Result": 0,
            "OrderId": order_id,
            "Symbol": symbol,
            "Side": side,
            "Qty": quantity,
            "Price": price,
        }
        self.orders.append(order)
        return order
