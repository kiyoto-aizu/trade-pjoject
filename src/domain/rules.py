import logging
from datetime import datetime, timedelta
from typing import List, Optional

from src.domain.enums import OrderSide, RankingType
from src.domain.models import OrderHistoryEntry, PriceLimit, ScoredCandidate, TradeSignal

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
    today = datetime.now().date()
    return any(
        entry.symbol == signal.symbol and entry.side == signal.side
        and _entry_date(entry) == today
        for entry in order_history
    )


def _entry_date(entry: OrderHistoryEntry):
    try:
        return datetime.fromisoformat(entry.timestamp).date()
    except ValueError:
        return None


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


def merge_ranking_candidates(turnover_ranking, price_gain_ranking):
    rankings = (turnover_ranking, price_gain_ranking)
    defaults = [len(ranking) + 1 for ranking in rankings]
    scores = {}
    for index, ranking in enumerate(rankings):
        for entry in ranking:
            scores.setdefault(entry.symbol, [defaults[0], defaults[1]])[index] = entry.rank
    return sorted(scores, key=lambda symbol: (sum(scores[symbol]), symbol))


def exclude_by_regulation(candidates, regulations):
    return [
        symbol for symbol in candidates
        if symbol in regulations
        and not regulations[symbol].is_restricted
        and getattr(regulations[symbol], "primary_exchange", 1) not in {3, 5, 6}
    ]


def limit_candidates(candidates, min_count=30, max_count=50):
    if len(candidates) < min_count:
        logger.warning("候補銘柄が%d件で、最小件数%d件を下回っています。", len(candidates), min_count)
    return candidates[:max_count]


def calculate_volume_surge_ratio(today_volume: float, average_volume: float) -> float:
    if average_volume <= 0:
        raise ValueError("average_volume must be positive")
    return today_volume / average_volume


def select_top_n_by_surge_ratio(scored: List[ScoredCandidate], n: int = 10):
    return [candidate.symbol for candidate in sorted(scored, key=lambda item: (-item.surge_ratio, item.symbol))[:n]]


def check_kill_switch(daily_orders, daily_pnl, capital, settings, order_amount):
    max_amount = min(settings.MAX_ORDER_AMOUNT_PER_TRADE, settings.API_SOFT_LIMIT)
    if order_amount > max_amount or daily_orders >= settings.MAX_ORDER_COUNT_PER_DAY:
        return False
    if capital > 0 and daily_pnl <= -(capital * settings.DAILY_LOSS_LIMIT_RATIO):
        return False
    return True
