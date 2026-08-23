from datetime import datetime

from src.domain.enums import RankingType
from src.domain.models import Regulation, ScreeningResult
from src.domain.rules import exclude_by_regulation, limit_candidates, merge_ranking_candidates


class ScreeningUseCase:
    def __init__(self, ranking_repository, regulation_repository, exchange_repository, result_repository, notifier=None):
        self.ranking_repository = ranking_repository
        self.regulation_repository = regulation_repository
        self.exchange_repository = exchange_repository
        self.result_repository = result_repository
        self.notifier = notifier

    def execute(self) -> ScreeningResult:
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
