from src.api import request_handler
from src.config import config


class BoardRepository:
    """kabuステーションの/board APIから板情報を取得します。"""

    def __init__(self, token: str):
        self.token = token

    @staticmethod
    def get_rate_limit_stats() -> dict[str, int]:
        return request_handler.get_rate_limit_stats("/board/")

    def get_current_board_with_freshness(self, symbol: str) -> dict | None:
        """現在値に加え、現値時刻とステータスを含む板情報を返します。"""
        response = request_handler.send_get(
            f"{config.BASE_URL}/board/{symbol}@1",
            headers={"X-API-KEY": self.token},
        )
        if not response:
            return None
        return {
            "symbol_name": response.get("SymbolName", f"銘柄:{symbol}"),
            "current_price": response.get("CurrentPrice"),
            "current_price_time": response.get("CurrentPriceTime"),
            "current_price_status": response.get("CurrentPriceStatus"),
            "trading_volume": response.get("TradingVolume"),
            "trading_value": response.get("TradingValue"),
            "response_keys": sorted(response.keys()),
            "trading_volume_time": response.get("TradingVolumeTime"),
            "vwap": response.get("VWAP"),
            "raw_trading_volume": response.get("TradingVolume"),
            "raw_trading_value": response.get("TradingValue"),
        }

    def get_current_board_for_diagnostics(self, symbol: str) -> dict | None:
        """診断記録用に、生返答の項目を含む板情報をそのまま返します。"""
        return self.get_current_board_with_freshness(symbol)

    def get_current_board(self, symbol: str) -> dict | None:
        """既存呼び出し向けに現在値・出来高などの板情報を返します。"""
        board = self.get_current_board_with_freshness(symbol)
        if board is None:
            return None
        return {
            "symbol_name": board["symbol_name"],
            "current_price": board["current_price"],
            "trading_volume": board["trading_volume"],
            "trading_value": board["trading_value"],
            "response_keys": board["response_keys"],
        }

    def get_current_price(self, symbol: str) -> float | None:
        """指定銘柄の現在値のみを取得します。"""
        board = self.get_current_board(symbol)
        price = board.get("current_price") if board else None
        return float(price) if price is not None else None
