"""実注文を発生させないペーパートレード用の注文実行実装。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from pathlib import Path

from src.config import config
from src.infrastructure.persistence.storage import read_json, write_json


@dataclass
class PaperOrderExecutor:
    """現在価格を使って仮想残高と保有株を更新する注文実行器。"""

    prices: Dict[str, float]
    cash: float = 1_000_000.0
    order_qty: int = field(default_factory=lambda: config.DEFAULT_ORDER_QTY)
    holdings: Dict[str, int] = field(default_factory=dict)
    orders: List[dict] = field(default_factory=list)
    state_path: Optional[Path] = None
    _next_order_id: int = 1

    def __post_init__(self) -> None:
        if self.state_path is None:
            return
        state = read_json(self.state_path)
        if not isinstance(state, dict):
            return
        self.cash = float(state.get('cash', self.cash))
        self.holdings = {
            str(symbol): int(quantity)
            for symbol, quantity in state.get('holdings', {}).items()
            if int(quantity) > 0
        }
        self._next_order_id = max(1, int(state.get('next_order_id', self._next_order_id)))

    def _save_state(self) -> None:
        if self.state_path is not None:
            write_json(self.state_path, {
                'cash': self.cash,
                'holdings': self.holdings,
                'next_order_id': self._next_order_id,
            })

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
        self._save_state()
        return order

    def get_wallet_cash(self, token: str) -> dict:
        del token
        return {'StockAccountWallet': self.cash}

    def get_positions(self, token: str) -> list[dict]:
        del token
        return [
            {
                'Symbol': symbol,
                'Side': config.OrderSide.SELL.value,
                'HoldQty': quantity,
                'ProfitLoss': 0,
            }
            for symbol, quantity in self.holdings.items()
        ]
