from src.api import request_handler
from src.config import config
from src.domain.models import Regulation


class RegulationRepository:
    def __init__(self, token: str):
        self.token = token

    def get_regulation(self, symbol: str, market_code: int, target_date=None) -> Regulation:
        # target_dateはHistoricalRegulationRepositoryとのインターフェース統一のために
        # 受け付けているだけで、本番(kabu STATION API)は常に現在時点の規制情報を返すため
        # 未使用のまま無視する(ADR-0001)。
        response = request_handler.send_get(
            f"{config.BASE_URL}/regulations/{symbol}@{market_code}",
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return Regulation(symbol, True, "規制情報取得失敗")
        restrictions = response.get("RegulationsInfo", [])
        restricted = bool(restrictions)
        reason = ", ".join(str(item.get("Reason", "")) for item in restrictions)
        return Regulation(symbol, restricted, reason)
