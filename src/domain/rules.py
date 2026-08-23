"""
================================================================================
ビジネスルール定義モジュール
取引に関する検証・計算ロジックを集中管理しています。
================================================================================
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from src.domain.enums import OrderSide, RankingType
from src.domain.models import OrderHistoryEntry, PriceLimit, ScoredCandidate, TradeSignal

logger = logging.getLogger(__name__)


# ================================================================================
# 価格基準値の計算
# ================================================================================

def calculate_price_limit(closes: List[float]) -> Optional[PriceLimit]:
    """
    過去の終値から移動平均を計算し、買い/売り基準値を決定します。
    
    Args:
        closes: 過去の終値のリスト
        
    Returns:
        買い基準値（移動平均の99%）と売り基準値（移動平均の101%）を含むPriceLimitオブジェクト
        データ不足の場合はNone
    """
    if not closes or len(closes) < 5:
        return None

    moving_average = sum(closes) / len(closes)
    return PriceLimit(
        buy=round(moving_average * 0.99, 1),
        sell=round(moving_average * 1.01, 1),
    )


# ================================================================================
# 注文重複チェック
# ================================================================================

def is_duplicate_order(signal: TradeSignal, order_history: List[OrderHistoryEntry]) -> bool:
    """
    同一銘柄・同一方向の注文が当日中に既に存在するかを確認します。
    
    Args:
        signal: チェック対象の取引シグナル
        order_history: 注文履歴のリスト
        
    Returns:
        重複が存在する場合True、存在しない場合False
    """
    today = datetime.now().date()
    return any(
        entry.symbol == signal.symbol and entry.side == signal.side
        and _entry_date(entry) == today
        for entry in order_history
    )


def _entry_date(entry: OrderHistoryEntry):
    """
    注文履歴エントリからタイムスタンプを抽出して日付に変換します。
    
    Args:
        entry: 注文履歴エントリ
        
    Returns:
        日付オブジェクト、パースエラーの場合はNone
    """
    try:
        return datetime.fromisoformat(entry.timestamp).date()
    except ValueError:
        return None


# ================================================================================
# 最近の注文チェック
# ================================================================================

def is_recent_order(signal: TradeSignal, order_history: List[OrderHistoryEntry], lock_seconds: int) -> bool:
    """
    指定秒数以内に同一銘柄・同一方向の注文が存在するかを確認します。
    二重発注防止のため、ロック期間内の注文をチェックします。
    
    Args:
        signal: チェック対象の取引シグナル
        order_history: 注文履歴のリスト
        lock_seconds: ロック期間（秒）
        
    Returns:
        ロック期間内に重複が存在する場合True、存在しない場合False
    """
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


# ================================================================================
# 注文実行可否判定
# ================================================================================

def is_safe_to_order(
    signal: TradeSignal,
    wallet_amount: Optional[float],
    has_holdings: bool,
    order_history: List[OrderHistoryEntry],
    lock_seconds: int,
) -> bool:
    """
    取引シグナルに基づいて注文を実行しても安全かどうかを総合的に判定します。
    
    チェック項目：
    - 買い注文の場合、十分な資金があるか
    - 売り注文の場合、保有株があるか
    - 当日同一銘柄の重複注文がないか
    - ロック期間内の重複注文がないか
    
    Args:
        signal: 実行予定の取引シグナル
        wallet_amount: 現物買付可能額
        has_holdings: 保有株の有無
        order_history: 注文履歴のリスト
        lock_seconds: ロック期間（秒）
        
    Returns:
        注文を実行しても安全な場合True、安全でない場合False
    """
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


# ================================================================================
# ランキング候補の統合・並べ替え
# ================================================================================

def merge_ranking_candidates(turnover_ranking, price_gain_ranking):
    """
    出来高ランキングと値上がり率ランキングを統合し、総合スコアで順位付けします。
    両ランキングで上位の銘柄が高スコアになります。
    
    Args:
        turnover_ranking: 出来高ランキングのリスト
        price_gain_ranking: 値上がり率ランキングのリスト
        
    Returns:
        統合スコアでソートされた銘柄シンボルのリスト
    """
    rankings = (turnover_ranking, price_gain_ranking)
    defaults = [len(ranking) + 1 for ranking in rankings]
    scores = {}
    for index, ranking in enumerate(rankings):
        for entry in ranking:
            scores.setdefault(entry.symbol, [defaults[0], defaults[1]])[index] = entry.rank
    return sorted(scores, key=lambda symbol: (sum(scores[symbol]), symbol))


# ================================================================================
# 規制・制限チェック
# ================================================================================

def exclude_by_regulation(candidates, regulations):
    """
    規制対象外の銘柄のみをフィルタリングして返します。
    
    除外条件：
    - 取引が制限されている銘柄
    - プライマリ取引所が3, 5, 6（指定市場など）である銘柄
    
    Args:
        candidates: フィルタリング対象の銘柄シンボルのリスト
        regulations: 銘柄ごとの規制情報を含む辞書
        
    Returns:
        規制対象外の銘柄シンボルのリスト
    """
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
