from datetime import date

from src.domain.enums import RankingType
from src.domain.models import RankingEntry


class HistoricalRankingRepository:
    """日付付き上場銘柄マスタと日足からランキングを生成します。"""

    def __init__(self, listed_security_repository, market_data_client):
        self.listed_security_repository = listed_security_repository
        self.market_data_client = market_data_client

    def get_ranking(
        self,
        ranking_type: RankingType,
        exchange_division: str = "ALL",
        target_date: date | None = None,
    ) -> list[RankingEntry]:
        if target_date is None:
            raise ValueError("HistoricalRankingRepositoryにはtarget_dateが必要です")

        entries = []
        for security in self.listed_security_repository.load_for_date(target_date):
            if exchange_division != "ALL" and security.exchange_division != exchange_division:
                continue
            metrics = self.market_data_client.get_daily_market_data(security.symbol, target_date)
            if not metrics:
                continue
            if ranking_type == RankingType.TURNOVER:
                value = metrics["close"] * metrics["volume"]
            else:
                if metrics["previous_close"] <= 0:
                    continue
                value = (metrics["close"] / metrics["previous_close"] - 1) * 100
            entries.append((security.symbol, float(value), float(metrics["close"])))

        entries.sort(key=lambda entry: (-entry[1], entry[0]))
        return [
            RankingEntry(symbol, index + 1, value, ranking_type, current_price)
            for index, (symbol, value, current_price) in enumerate(entries)
        ]