"""infrastructure/kabu/get_board.py"""
import logging
from src.config import config
from src.api import request_handler

logger = logging.getLogger(__name__)


def get_current_board(token, symbol):
    """
    kabuステーションの/board APIから現在の株価情報を取得する関数
    """
    url = f"{config.BASE_URL}/board/{symbol}@1"
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }
    
    # 💡 共通のGETハンドラーを使用
    res_json = request_handler.send_get(url, headers=headers)
    
    if not res_json:
        return None
    
    logger.info("📊 [板情報取得] 銘柄: %s | 現在値: %s", symbol, res_json.get('CurrentPrice'))
    # メイン側で使いやすいように、必要なデータだけを辞書で返す
    return {
        "symbol_name": res_json.get('SymbolName', f"銘柄:{symbol}"),
        "current_price": res_json.get('CurrentPrice')
    }