import logging
from datetime import datetime, timedelta
from typing import List, Optional

from src.domain.enums import OrderSide
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal

logger = logging.getLogger(__name__)


def calculate_price_limit(closes: List[float]) -> Optional[PriceLimit]:
    if not closes or len(closes) < 5:
        return None

    moving_average = sum(closes) / len(closes)
    return PriceLimit(
        buy=round(moving_average * 0.99, 1),
        sell=round(moving_average * 1.01, 1),
    )


def is_duplicate_order(signal: TradeSignal, order_history: List[OrderHistoryEntry]) -> bool:
    return any(
        entry.symbol == signal.symbol and entry.side == signal.side
        for entry in order_history
    )


def is_recent_order(signal: TradeSignal, order_history: List[OrderHistoryEntry], lock_seconds: int) -> bool:
    cutoff = datetime.now() - timedelta(seconds=lock_seconds)
    for entry in reversed(order_history):
        if entry.symbol == signal.symbol and entry.side == signal.side:
            try:
                ts = datetime.fromisoformat(entry.timestamp)
                if ts >= cutoff:
                    return True
            except ValueError:
                logging.getLogger(__name__).debug("不正なタイムスタンプをスキップします: %s", entry.timestamp)
                continue
    return False


def is_safe_to_order(
    signal: TradeSignal,
    wallet_amount: Optional[float],
    has_holdings: bool,
    order_history: List[OrderHistoryEntry],
    lock_seconds: int,
) -> bool:
    if signal.side == OrderSide.BUY:
        if wallet_amount is None:
            logger.warning("現物買付可能額が不明なため、買い注文を見送ります。")
            return False

        required = signal.price * signal.qty
        if required > wallet_amount:
            logger.warning("予算不足: 注文必要額 %s 円 > 買付可能額 %s 円", required, wallet_amount)
            return False

    if signal.side == OrderSide.SELL and not has_holdings:
        logger.warning("保有株が確認できないため、売り注文を見送ります。")
        return False

    if is_duplicate_order(signal, order_history):
        logger.warning("当日同一シンボル・同一方向の注文が既にあります。")
        return False

    if is_recent_order(signal, order_history, lock_seconds):
        logger.warning("同一注文が %s 秒以内に発生しています。二重発注を防止します。", lock_seconds)
        return False

    return True
