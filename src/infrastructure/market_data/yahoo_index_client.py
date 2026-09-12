"""Yahoo Financeから市場指数の日足OHLCを取得します。"""
import logging
from datetime import datetime, timedelta, timezone

from src.api import request_handler
from src.domain.market_volatility import MarketDailyBar

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


class YahooIndexClient:
    """.Tを付けずにYahoo Financeの指数シンボルを取得するクライアント。"""

    SUPPORTED_SYMBOLS = frozenset({"^N225", "^VIX", "^DJI", "^GSPC"})

    def get_daily_ohlc(self, symbol: str, range_: str = "max") -> list[MarketDailyBar]:
        """指定指数の確定日足OHLCを返します。"""
        if symbol not in self.SUPPORTED_SYMBOLS:
            raise ValueError(f"未対応の指数シンボルです: {symbol}")

        response = request_handler.send_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params=(
                {"interval": "1d", "period1": 0, "period2": int(datetime.now(timezone.utc).timestamp())}
                if range_ == "max"
                else {"interval": "1d", "range": range_}
            ),
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        if not response:
            return []

        try:
            result = response["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            quote = result["indicators"]["quote"][0]
            rows = []
            today = datetime.now(JST).date()
            for timestamp, open_, high, low, close in zip(
                timestamps,
                quote.get("open", []),
                quote.get("high", []),
                quote.get("low", []),
                quote.get("close", []),
            ):
                target_date = datetime.fromtimestamp(timestamp, tz=JST).date()
                if target_date >= today or any(value is None for value in (open_, high, low, close)):
                    continue
                rows.append(MarketDailyBar(
                    date=target_date,
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                ))
            return rows
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            logger.warning("Yahoo Financeの指数日足取得に失敗しました: %s", symbol)
            return []
