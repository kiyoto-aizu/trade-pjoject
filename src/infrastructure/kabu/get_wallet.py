from src.config import config
from src.api import request_handler

def get_wallet_cash(token):
    """現物買付可能額を取得する。"""
    url = f"{config.BASE_URL}/wallet/cash"
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }
    return request_handler.send_get(url, headers=headers)
