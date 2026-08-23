from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from src.domain.enums import OrderSide, RankingType


@dataclass
class OrderHistoryEntry:
    symbol: str
    side: OrderSide
    price: float
    qty: int
    timestamp: str

    @classmethod
    def from_dict(cls, data: Dict) -> 'OrderHistoryEntry':
        return cls(
            symbol=data['symbol'],
            side=OrderSide(data['side']),
            price=data['price'],
            qty=data['qty'],
            timestamp=data['timestamp'],
        )

    def to_dict(self) -> Dict:
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'price': self.price,
            'qty': self.qty,
            'timestamp': self.timestamp,
        }


@dataclass
class PriceLimit:
    buy: float
    sell: float


@dataclass
class TradeSignal:
    symbol: str
    side: OrderSide
    price: float
    qty: int

    @classmethod
    def evaluate(cls, symbol: str, current_price: float, limit: PriceLimit) -> Optional['TradeSignal']:
        if current_price <= limit.buy:
            return cls(symbol=symbol, side=OrderSide.BUY, price=current_price, qty=100)
        if current_price >= limit.sell:
            return cls(symbol=symbol, side=OrderSide.SELL, price=current_price, qty=100)
        return None

    def to_order_history_entry(self) -> 'OrderHistoryEntry':
        return OrderHistoryEntry(
            symbol=self.symbol,
            side=self.side,
            price=self.price,
            qty=self.qty,
            timestamp=datetime.now().isoformat(),
        )


@dataclass
class RankingEntry:
    symbol: str
    rank: int
    value: float
    ranking_type: RankingType


@dataclass
class Regulation:
    symbol: str
    is_restricted: bool
    reason: str = ""
    primary_exchange: int = 1


@dataclass
class ScreeningResult:
    date: str
    symbols: list[str]
    generated_at: str


@dataclass
class ScoredCandidate:
    symbol: str
    today_volume: float
    average_volume: float
    surge_ratio: float


@dataclass
class FilteringResult:
    date: str
    symbols: list[str]
    generated_at: str
