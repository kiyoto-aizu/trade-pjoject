"""
================================================================================
スクリーニングユースケース
株式銘柄の初期選別を行うアプリケーションロジック層です。
ランキング情報から規制対象外の銘柄を抽出し、スクリーニング結果として出力します。
================================================================================
"""
from datetime import datetime
import logging
from time import sleep

from src.config import config
from src.domain.enums import RankingType
from src.domain.models import Regulation, ScreeningAuditEntry, ScreeningResult
from src.domain.rules import exclude_by_regulation, limit_candidates, merge_ranking_candidates

logger = logging.getLogger(__name__)


class ScreeningUseCase:
    """
    スクリーニング処理を実行するユースケッククラス。
    
    出来高と値上がり率のランキングを結合し、規制情報を確認した上で
    取引対象銘柄のリストを生成します。
    """
    
    def __init__(self, ranking_repository, regulation_repository, exchange_repository, result_repository, notifier=None):
        """
        ScreeningUseCaseを初期化します。
        
        Args:
            ranking_repository: ランキング情報を取得するリポジトリ
            regulation_repository: 規制情報を取得するリポジトリ
            exchange_repository: 取引所情報を取得するリポジトリ
            result_repository: スクリーニング結果を保存するリポジトリ
            notifier: 通知機能（オプション）
        """
        self.ranking_repository = ranking_repository
        self.regulation_repository = regulation_repository
        self.exchange_repository = exchange_repository
        self.result_repository = result_repository
        self.notifier = notifier

    def execute(self) -> ScreeningResult:
        """
        スクリーニング処理を実行します。
        
        処理フロー：
        1. 出来高と値上がり率のランキングを取得
        2. 両ランキングを統合して候補銘柄を作成
        3. 各銘柄の規制情報と取引所情報を確認
        4. 規制対象外の銘柄に絞り込み
        5. 結果を保存して返却
        
        Returns:
            ScreeningResultオブジェクト
            
        Raises:
            RuntimeError: ランキングが空の場合
        """
        turnover = self.ranking_repository.get_ranking(RankingType.TURNOVER)
        price_gain = self.ranking_repository.get_ranking(RankingType.PRICE_GAIN)
        if not turnover or not price_gain:
            if self.notifier:
                self.notifier("ランキングが空のためスクリーニングを中止しました")
            raise RuntimeError("ランキングが空のためスクリーニングを中止しました")
        candidates = merge_ranking_candidates(turnover, price_gain)
        turnover_by_symbol = {entry.symbol: entry for entry in turnover}
        price_gain_by_symbol = {entry.symbol: entry for entry in price_gain}
        regulations = {}
        for symbol in candidates:
            exchange = self.exchange_repository.get_primary_exchange(symbol)
            sleep(config.API_REQUEST_INTERVAL_SECONDS)
            if exchange is None:
                regulations[symbol] = Regulation(symbol, True, "優先市場情報取得失敗", 0)
                continue
            regulation = self.regulation_repository.get_regulation(symbol, exchange)
            sleep(config.API_REQUEST_INTERVAL_SECONDS)
            regulations[symbol] = Regulation(
                symbol=symbol,
                is_restricted=regulation.is_restricted,
                reason=regulation.reason,
                primary_exchange=exchange,
            )
        exclusion_result = exclude_by_regulation(candidates, regulations)
        symbols = limit_candidates(exclusion_result.remaining)
        selected_symbols = set(symbols)
        default_turnover_rank = len(turnover) + 1
        default_price_gain_rank = len(price_gain) + 1
        audit_entries = []
        for symbol in candidates:
            turnover_entry = turnover_by_symbol.get(symbol)
            price_gain_entry = price_gain_by_symbol.get(symbol)
            turnover_rank = turnover_entry.rank if turnover_entry else default_turnover_rank
            price_gain_rank = price_gain_entry.rank if price_gain_entry else default_price_gain_rank
            regulation = regulations[symbol]
            audit_entries.append(ScreeningAuditEntry(
                symbol=symbol,
                turnover_rank=turnover_rank,
                turnover_value=turnover_entry.value if turnover_entry else 0.0,
                price_gain_rank=price_gain_rank,
                price_gain_value=price_gain_entry.value if price_gain_entry else 0.0,
                total_rank=turnover_rank + price_gain_rank,
                primary_exchange=regulation.primary_exchange,
                is_restricted=regulation.is_restricted,
                restriction_reason=regulation.reason,
                selected=symbol in selected_symbols,
            ))
        result = ScreeningResult(
            datetime.now().date().isoformat(), symbols, datetime.now().isoformat(), audit_entries
        )
        self.result_repository.save(result)
        if self.notifier:
            self._notify_completion(
                candidates,
                exclusion_result,
                symbols,
                turnover_by_symbol,
                price_gain_by_symbol,
            )
        return result

    def _notify_completion(
        self,
        candidates,
        exclusion_result,
        symbols,
        turnover_by_symbol,
        price_gain_by_symbol,
    ) -> None:
        message = (
            f"スクリーニング完了: {len(symbols)}銘柄（候補{len(candidates)}件中、"
            f"規制{exclusion_result.excluded_by_regulation_count}件・"
            f"地方取引所{exclusion_result.excluded_by_exchange_count}件を除外）"
        )
        top_entries = []
        for symbol in exclusion_result.remaining[:3]:
            turnover_entry = turnover_by_symbol.get(symbol)
            price_gain_entry = price_gain_by_symbol.get(symbol)
            if price_gain_entry and (not turnover_entry or price_gain_entry.rank <= turnover_entry.rank):
                top_entries.append(f"{symbol}(値上がり率+{price_gain_entry.value:g}%)")
            elif turnover_entry:
                top_entries.append(f"{symbol}(売買代金{turnover_entry.value / 100_000_000:g}億)")
        if top_entries:
            message += "\n上位: " + " / ".join(top_entries)
        try:
            self.notifier(message)
        except Exception:
            logger.exception("スクリーニング完了通知に失敗しました。")
