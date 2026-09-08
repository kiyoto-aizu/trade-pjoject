"""infrastructure/market_data/get_5d_closes.py"""
import logging
from datetime import datetime, timedelta, timezone

from src.api import request_handler

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

def get_yahoo_5d_closes(symbol):
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    # range=5dだと休場日で確定終値が5本未満になり得るため多めに取得する
    params = {"interval": "1d", "range": "10d"}

    # 💡 共通化したGET処理を呼び出す
    res_json = request_handler.send_get(url, params=params, headers=headers, timeout=30)
    
    if not res_json:
        return []

    try:
        result = res_json.get("chart", {}).get("result", [])
        if not result:
            return []

        chart_result = result[0]
        timestamps = chart_result.get("timestamp", [])
        quote = chart_result.get("indicators", {}).get("quote", [{}])[0]
        raw_closes = quote.get("close", [])

        # 当日分の最終バーは取引時間中リアルタイムに追随する未確定値のため除外する
        today = datetime.now(JST).date()
        closes = [
            float(close)
            for ts, close in zip(timestamps, raw_closes)
            if close is not None and datetime.fromtimestamp(ts, JST).date() < today
        ]
        return closes[-5:]
    except (KeyError, TypeError, ValueError) as e:
        logger.exception("Yahooデータの解析に失敗しました (%s): %s", symbol, e)
        return []