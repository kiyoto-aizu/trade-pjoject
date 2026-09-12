"""Yahoo FinanceからRSI計算用の確定日足終値を取得します。"""
import logging
from datetime import datetime, timedelta, timezone

from src.api import request_handler
from src.domain.volatility import DailyBar

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


def get_yahoo_daily_bars(symbol: str) -> list[DailyBar]:
    """Yahoo Financeから当日未確定足を除いた日足OHLCを取得します。"""
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    params = {"interval": "1d", "range": "60d"}
    response = request_handler.send_get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    if not response:
        return []

    try:
        result = response.get("chart", {}).get("result", [])
        if not result:
            return []
        chart_result = result[0]
        timestamps = chart_result.get("timestamp", [])
        quote = chart_result.get("indicators", {}).get("quote", [{}])[0]
        raw_highs = quote.get("high", [])
        raw_lows = quote.get("low", [])
        raw_closes = quote.get("close", [])
        today = datetime.now(JST).date()
        return [
            DailyBar(high=float(high), low=float(low), close=float(close))
            for timestamp, high, low, close in zip(timestamps, raw_highs, raw_lows, raw_closes)
            if high is not None and low is not None and close is not None
            and datetime.fromtimestamp(timestamp, JST).date() < today
        ]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.exception("Yahooデータの解析に失敗しました (%s): %s", symbol, exc)
        return []


def get_yahoo_daily_closes(symbol: str) -> list[float]:
    """既存利用者向けにYahoo Financeの日足終値だけを返します。"""
    yf_symbol = f"{symbol}.T"
    response = request_handler.send_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}",
        params={"interval": "1d", "range": "60d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    if not response:
        return []
    try:
        result = response.get("chart", {}).get("result", [])
        if not result:
            return []
        chart_result = result[0]
        timestamps = chart_result.get("timestamp", [])
        raw_closes = chart_result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        today = datetime.now(JST).date()
        return [
            float(close)
            for timestamp, close in zip(timestamps, raw_closes)
            if close is not None and datetime.fromtimestamp(timestamp, JST).date() < today
        ]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.exception("Yahoo終値データの解析に失敗しました (%s): %s", symbol, exc)
        return []