import logging

from src.api import request_handler

logger = logging.getLogger(__name__)


class YahooFinanceClient:
    def get_average_volume(self, symbol: str, days: int = 20) -> float | None:
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
