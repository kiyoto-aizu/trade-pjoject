import logging

from src.api import request_handler

logger = logging.getLogger(__name__)


class YahooFinanceClient:
    def get_average_volume(self, symbol: str, days: int = 20) -> float | None:
    """過去の日足出来高平均を返します（後方互換用）。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": "1mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        try:
            volumes = response["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
            values = [float(volume) for volume in volumes if volume is not None][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの出来高取得に失敗しました: %s", symbol)
            return None

    def get_average_turnover(self, symbol: str, days: int = 20) -> float | None:
        """過去の日足終値と出来高から売買代金平均を返します。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": "1mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        try:
            quote = response["chart"]["result"][0]["indicators"]["quote"][0]
            values = [
                float(close) * float(volume)
                for close, volume in zip(quote["close"], quote["volume"])
                if close is not None and volume is not None
            ][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの売買代金取得に失敗しました: %s", symbol)
            return None
