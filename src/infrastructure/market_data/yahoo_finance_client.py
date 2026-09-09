import logging
from datetime import date, datetime, timezone

from src.api import request_handler

logger = logging.getLogger(__name__)


class YahooFinanceClient:
    def get_daily_market_data(self, symbol: str, target_date: date) -> dict[str, float] | None:
        """指定日と直前営業日の終値、および指定日の出来高を返します。"""
        try:
            response = request_handler.send_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
                params={"interval": "1d", "range": "3mo"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            result = response["chart"]["result"][0]
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            rows = [
                (datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(), close, volume)
                for timestamp, close, volume in zip(timestamps, quote["close"], quote["volume"])
                if close is not None and volume is not None
            ]
            target_index = next((index for index, row in enumerate(rows) if row[0] == target_date.isoformat()), None)
            if target_index is None or target_index == 0:
                return None
            _, close, volume = rows[target_index]
            previous_close = rows[target_index - 1][1]
            return {"close": float(close), "volume": float(volume), "previous_close": float(previous_close)}
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("Yahoo Financeの日足取得に失敗しました: %s %s", symbol, target_date)
            return None

    def _get_daily_turnover(self, symbol: str, days: int = 90) -> dict[str, float]:
        """Yahoo Financeの日足から日付別の売買代金を取得します。"""
        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.T",
            params={"interval": "1d", "range": f"{days}d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        result = response["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result["indicators"]["quote"][0]
        return {
            datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(): float(close) * float(volume)
            for timestamp, close, volume in zip(timestamps, quote["close"], quote["volume"])
            if close is not None and volume is not None
        }

    def get_turnover_for_date(self, symbol: str, target_date: date) -> float | None:
        """指定日の終値×出来高を返します。"""
        try:
            return self._get_daily_turnover(symbol).get(target_date.isoformat())
        except (KeyError, IndexError, TypeError, ValueError):
            logger.warning("Yahoo Financeの対象日売買代金取得に失敗しました: %s %s", symbol, target_date)
            return None

    def get_average_turnover_before(self, symbol: str, target_date: date, days: int = 20) -> float | None:
        """指定日より前の日足売買代金の平均を返します。"""
        try:
            values = [
                value for day, value in self._get_daily_turnover(symbol, days=days + 30).items()
                if day < target_date.isoformat()
            ][-days:]
            return sum(values) / len(values) if values else None
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
            logger.warning("Yahoo Financeの対象日前平均売買代金取得に失敗しました: %s %s", symbol, target_date)
            return None

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
