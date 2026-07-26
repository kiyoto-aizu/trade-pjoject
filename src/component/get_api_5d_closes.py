# component/get_5d_closes.py
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import request_handler

def get_yahoo_5d_closes(symbol):
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"interval": "1d", "range": "5d"}

    # 💡 共通化したGET処理を呼び出す
    res_json = request_handler.send_get(url, params=params, headers=headers, timeout=30)
    
    if not res_json:
        return []

    try:
        result = res_json.get("chart", {}).get("result", [])
        if not result:
            return []

        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [float(close) for close in quote.get("close", []) if close is not None]
        return closes[-5:]
    except Exception as e:
        print(f"⚠️ Yahooデータの解析に失敗しました ({symbol}): {e}")
        return []