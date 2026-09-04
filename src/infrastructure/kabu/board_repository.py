from src.api import request_handler
from src.config import config


class BoardRepository:
    """kabuステーションの/board APIから板情報を取得します。"""

    def __init__(self, token: str):
        self.token = token

    def get_current_board(self, symbol: str) -> dict | None:
        """指定銘柄の現在値・出来高などの板情報を取得します。"""
        response = request_handler.send_get(
            f"{config.BASE_URL}/board/{symbol}@1",
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return None
        return {
            "symbol_name": response.get("SymbolName", f"銘柄:{symbol}"),
            "current_price": response.get("CurrentPrice"),
            "trading_volume": response.get("TradingVolume"),
        }

    def get_current_price(self, symbol: str) -> float | None:
        """指定銘柄の現在値のみを取得します。"""
        board = self.get_current_board(symbol)
        price = board.get("current_price") if board else None
        return float(price) if price is not None else None
