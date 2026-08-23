"""
================================================================================
注文送信モジュール
Kabu.com Station APIを使用して市場注文を実行します。
================================================================================
"""
import logging
from src.config import config
from src.api import request_handler

logger = logging.getLogger(__name__)


def place_market_order(token, symbol, side):
    """
    Kabu.com Station APIを使用して成行注文を実行します。
    
    Args:
        token: APIトークン
        symbol: 銘柄コード (例: "7203")
        side: 売買区分 ("1" = 売、"2" = 買)
        
    Returns:
        API応答JSON、エラー時はNone
        
    Note:
        - 注文タイプ: 成行（当日限り）
        - 取引所: 東証(1)
        - 現物取引: CashMargin=1（現物）
        - デリバリー: 売却時=0（売却代金受け取り）、買付時=2（買付代金支払い）
    """
    url = f"{config.BASE_URL}/sendorder"
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }
    
    # Kabu.com Station API公式仕様に沿った注文データ
    # 検証用環境で安全に即座に約定させるため、成行・当日限りの設定にしています
    deliv_type = 2 if side == "2" else 0
    fund_type = "AA" if side == "2" else "  "

    qty = getattr(config, 'DEFAULT_ORDER_QTY', 100)
    order_data = {
        "Password": config.API_PASSWORD,  # APIパスワード
        "Symbol": symbol,                  # 銘柄コード
        "Exchange": 1,                     # 1 = 東証
        "SecurityType": 1,                 # 1 = 現物
        "Side": side,                      # "1" = 売、"2" = 買
        "CashMargin": 1,                   # 1 = 現物
        "DelivType": deliv_type,           # デリバリータイプ
        "FundType": fund_type,             # 資金タイプ
        "AccountType": 4,                  # 4 = 特定口座
        "Qty": qty,                        # 注文数量（日本株は基本100株単位）
        "FrontOrderType": 10,              # 10 = 成行
        "Price": 0,                        # 成行の場合は0を指定
        "ExpireDay": 0,                    # 0 = 当日限り
    }

    logger.info("📣 [発注要求] 銘柄: %s | 区分: %s | 数量: %s株", symbol, '買' if side == '2' else '売', qty)
    logger.debug("📦 発注ペイロード: %s", order_data)
    
    # 共通POSTハンドラーを呼び出し
    res_json = request_handler.send_post(url, data=order_data, headers=headers)
    return res_json
    
    if res_json and res_json.get('Result') == 0:
        order_id = res_json.get('OrderId')
        logger.info("🎯 【発注成功】 注文受付番号(OrderId): %s", order_id)
        return order_id
    else:
        logger.error("❌ 【発注失敗】 APIからの応答: %s", res_json)
        return None