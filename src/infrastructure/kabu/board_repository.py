from src.api import request_handler
from src.config import config


class BoardRepository:
    """kabuステーションの板情報から現在値を取得します。"""

    def __init__(self, token: str):
        self.token = token

    def get_current_price(self, symbol: str) -> float | None:
        response = request_handler.send_get(
            f"{config.BASE_URL}/board/{symbol}@1",
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return None
        price = response.get("CurrentPrice")
        return float(price) if price is not None else None
