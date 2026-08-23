from src.api import request_handler
from src.config import config


class PrimaryExchangeRepository:
    def __init__(self, token: str):
        self.token = token

    def get_primary_exchange(self, symbol: str) -> int | None:
        response = request_handler.send_get(
            f"{config.BASE_URL}/primaryexchange/{symbol}",
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return None
        value = response.get("PrimaryExchange")
        return int(value) if value is not None else None
