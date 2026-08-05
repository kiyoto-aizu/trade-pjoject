import config
from api import request_handler

def get_positions(token, product='1', symbol=None, side=None, addinfo='true'):
    """保有建玉（残高）を取得する。"""
    url = f"{config.BASE_URL}/positions"
    headers = {
        'Content-Type': 'application/json',
        'X-API-KEY': token
    }
    params = {
        'product': product,
        'addinfo': addinfo
    }
    if symbol is not None:
        params['symbol'] = symbol
    if side is not None:
        params['side'] = side
    return request_handler.send_get(url, params=params, headers=headers)
