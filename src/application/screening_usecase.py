"""
================================================================================
スクリーニングユースケース
株式銘柄の初期選別を行うアプリケーションロジック層です。
ランキング情報から規制対象外の銘柄を抽出し、スクリーニング結果として出力します。
================================================================================
"""
from datetime import datetime

from src.domain.enums import RankingType
from src.domain.models import Regulation, ScreeningResult
from src.domain.rules import exclude_by_regulation, limit_candidates, merge_ranking_candidates


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
        regulations = {}
        for symbol in candidates:
            regulation = self.regulation_repository.get_regulation(symbol)
            exchange = self.exchange_repository.get_primary_exchange(symbol)
            regulations[symbol] = Regulation(
                symbol=symbol,
                is_restricted=regulation.is_restricted or exchange is None,
                reason=regulation.reason,
                primary_exchange=exchange or 0,
            )
        symbols = limit_candidates(exclude_by_regulation(candidates, regulations))
        result = ScreeningResult(datetime.now().date().isoformat(), symbols, datetime.now().isoformat())
        self.result_repository.save(result)
        if self.notifier:
            self.notifier("スクリーニング完了: " + ", ".join(symbols))
        return result
