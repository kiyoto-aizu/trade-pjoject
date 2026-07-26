# component/send_order.py
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from api import request_handler

def place_market_order(token, symbol, side):
    """
    kabuステーションAPIを使って「成行注文」を出す関数
    :param token: APIトークン
    :param symbol: 銘柄コード (例: "7203")
    :param side: 売買区分 ("1" = 売、"2" = 買)
    """
    url = f"{config.BASE_URL}/sendorder"
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }
    
    # 💡 kabuステーションAPIの公式仕様に沿った注文データ（リクエストボディ）
    # ※検証用環境で安全に即座に約定させるため、成行・当日限りの設定にしています
    # sell order on cash should use DelivType=0 and FundType='  ' (two spaces)
    # buy order on cash may use DelivType=2 and FundType='AA'
    deliv_type = 2 if side == "2" else 0
    fund_type = "AA" if side == "2" else "  "

    qty = getattr(config, 'DEFAULT_ORDER_QTY', 100)
    order_data = {
        "Password": config.API_PASSWORD, # configに設定したAPIパスワード
        "Symbol": symbol,
        "Exchange": 1,           # 1 = 東証
        "SecurityType": 1,       # 1 = 現物
        "Side": side,            # "1" = 売、"2" = 買
        "CashMargin": 1,         # 1 = 現物
        "DelivType": deliv_type,
        "FundType": fund_type,
        "AccountType": 4,        # 4 = 特定
        "Qty": qty,              # 💡 テスト単元数（日本株は基本100株単位）
        "FrontOrderType": 10,    # 10 = 成行
        "Price": 0,              # 成行の場合は0を指定
        "ExpireDay": 0,          # 0 = 当日限り
    }

    print(f"📣 [発注要求] 銘柄: {symbol} | 区分: {'買' if side == '2' else '売'} | 数量: {qty}株")
    print("📦 発注ペイロード:", order_data)
    
    # 共通POSTハンドラーに丸投げ
    res_json = request_handler.send_post(url, data=order_data, headers=headers)
    
    if res_json and res_json.get('Result') == 0:
        order_id = res_json.get('OrderId')
        print(f"🎯 【発注成功】 注文受付番号(OrderId): {order_id}")
        return order_id
    else:
        print(f"❌ 【発注失敗】 APIからの応答: {res_json}")
        return None