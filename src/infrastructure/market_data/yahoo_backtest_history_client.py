"""バックテスト用のYahoo Finance履歴データ取得。"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timezone

import requests

from src.domain.volatility import DailyBar

logger = logging.getLogger(__name__)


def _to_yahoo_ticker(symbol: str) -> str:
    """日本株コードをYahoo Financeのticker形式へ変換します。"""
    symbol_text = str(symbol).upper()
    if re.fullmatch(r"\d{3,4}[A-Z]?", symbol_text):
        return f"{symbol_text}.T"
    return symbol_text


def fetch_yahoo_history(symbols: list[str], days: int = 30) -> dict[str, list[float]]:
    """Yahoo Finance から最新の終値履歴を取得して、銘柄ごとの価格一覧を返す。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    history: dict[str, list[float]] = {}
    for symbol in symbols:
        symbol_text = str(symbol)
        ticker = _to_yahoo_ticker(symbol_text)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        payload = None
        try:
            response = requests.get(url, params={"interval": "1d", "range": f"{days}d"}, headers=headers, timeout=30)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            if hasattr(response, "json") and callable(response.json):
                payload = response.json()
            elif hasattr(response, "read"):
                raw = response.read()
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            else:
                raise ValueError("unsupported response type")
        except Exception as exc:
            logger.warning("%s の価格取得(requests)に失敗しました: %s", symbol_text, exc)
            try:
                request = urllib.request.Request(
                    url + f"?interval=1d&range={days}d",
                    headers=headers,
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as fallback_exc:
                logger.warning("%s の価格取得(urllib)に失敗しました: %s", symbol_text, fallback_exc)
                continue

        result = payload.get("chart", {}).get("result", [])
        if not result:
            continue

        closes: list[float] = []
        for quote in result[0].get("indicators", {}).get("quote", []):
            for close in quote.get("close", []):
                if close is not None:
                    closes.append(float(close))

        if closes:
            history[symbol_text] = closes[-days:]
    return history


def fetch_yahoo_dated_history(symbols: list[str], days: int = 90) -> dict[str, dict[str, float]]:
    """Yahoo Financeから日付付きの日足終値を取得します。"""
    dated_history: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        symbol_text = str(symbol)
        ticker = _to_yahoo_ticker(symbol_text)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        payload = None
        try:
            response = requests.get(
                url,
                params={"interval": "1d", "range": f"{days}d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("%s の日付付き価格取得に失敗しました: %s", symbol_text, exc)
            continue

        result = payload.get("chart", {}).get("result", [])
        if not result:
            continue
        timestamps = result[0].get("timestamp", [])
        quotes = result[0].get("indicators", {}).get("quote", [])
        closes = quotes[0].get("close", []) if quotes else []
        history = {
            datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(): float(close)
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        }
        if history:
            dated_history[symbol_text] = history
    return dated_history


def fetch_yahoo_dated_ohlc(symbols: list[str], days: int = 90) -> dict[str, dict[str, DailyBar]]:
    """Yahoo Financeから日付付きの日足OHLCを取得します。"""
    ohlc_history: dict[str, dict[str, DailyBar]] = {}
    for symbol in symbols:
        ticker = _to_yahoo_ticker(str(symbol))
        try:
            response = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": f"{days}d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json().get("chart", {}).get("result", [])
            if not result:
                continue
            chart = result[0]
            timestamps = chart.get("timestamp", [])
            quote = chart.get("indicators", {}).get("quote", [{}])[0]
            highs = quote.get("high", [])
            lows = quote.get("low", [])
            closes = quote.get("close", [])
            history = {
                datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(): DailyBar(
                    high=float(high), low=float(low), close=float(close),
                )
                for timestamp, high, low, close in zip(timestamps, highs, lows, closes)
                if high is not None and low is not None and close is not None
            }
            if history:
                ohlc_history[str(symbol)] = history
        except (requests.RequestException, KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning("%s の日付付きOHLC取得に失敗しました: %s", symbol, exc)
    return ohlc_history
