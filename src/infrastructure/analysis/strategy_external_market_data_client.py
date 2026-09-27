from __future__ import annotations

import logging
from datetime import date
from typing import Any
from urllib.parse import quote

from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient

logger = logging.getLogger(__name__)


class StrategyExternalMarketDataClient:
    """Yahoo Financeの許可済み指数OHLCを仮説期間に絞って取得する。"""

    def __init__(self, index_client: YahooIndexClient | None = None):
        self.index_client = index_client or YahooIndexClient()

    def load_index_data(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, Any]:
        results = []
        for symbol in dict.fromkeys(symbols):
            source_url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{quote(symbol, safe='^')}"
            )
            if symbol not in self.index_client.SUPPORTED_SYMBOLS:
                results.append({
                    "symbol": symbol,
                    "url": source_url,
                    "status": "unsupported_symbol",
                    "rows": [],
                })
                continue
            try:
                bars = self.index_client.get_daily_ohlc(symbol, range_="1y")
                rows = [
                    {
                        "date": bar.date.isoformat(),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                    }
                    for bar in bars
                    if start <= bar.date <= end
                ]
                results.append({
                    "symbol": symbol,
                    "url": source_url,
                    "status": "available" if rows else "no_data_in_period",
                    "rows": rows,
                })
            except Exception as exc:
                logger.warning("外部指数データの取得に失敗しました: %s (%s)", symbol, exc)
                results.append({
                    "symbol": symbol,
                    "url": source_url,
                    "status": "fetch_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "rows": [],
                })
        return {
            "provider": "Yahoo Finance Chart API",
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "results": results,
        }