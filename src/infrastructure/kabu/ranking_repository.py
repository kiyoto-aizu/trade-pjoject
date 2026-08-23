from src.api import request_handler
from src.config import config
from src.domain.enums import RankingType
from src.domain.models import RankingEntry


class RankingRepository:
    def __init__(self, token: str):
        self.token = token

    def get_ranking(self, ranking_type: RankingType):
        response = request_handler.send_get(
            f"{config.BASE_URL}/ranking",
            params={"Type": ranking_type.value},
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return []
        entries = response if isinstance(response, list) else response.get("Ranking", [])
        return [
            RankingEntry(
                symbol=str(item.get("Symbol")),
                rank=int(item.get("CurrentPriceRank", index + 1)),
                value=float(item.get("CurrentPrice", item.get("TradingValue", 0)) or 0),
                ranking_type=ranking_type,
            )
            for index, item in enumerate(entries)
            if item.get("Symbol")
        ]
