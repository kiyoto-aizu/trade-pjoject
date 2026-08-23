"""
================================================================================
Kabu.com Station APIトークン取得モジュール
API認証に必要なトークンをポータルから取得します。
================================================================================
"""
from src.config import config
from src.api import request_handler


def get_api_token():
    """
    Kabu.com Station APIのトークンを取得します。
    
    API_PASSWORDを使用してトークンエンドポイントにPOSTリクエストを送信し、
    返されたトークンを取得します。
    
    Returns:
        トークン文字列、取得失敗時はNone
    """
    url = f"{config.BASE_URL}/token"
    headers = {'Content-Type': 'application/json'}
    data = {"APIPassword": config.API_PASSWORD}

    # 共通化したPOST処理を呼び出す
    res_json = request_handler.send_post(url, data=data, headers=headers)

    if res_json:
        return res_json.get('Token')
    return None