"""
================================================================================
HTTP通信ハンドラモジュール
API通信の共通化・例外処理を一元管理します。
================================================================================
"""
import logging
import requests

logger = logging.getLogger(__name__)


def send_post(url, data=None, headers=None, timeout=10):
    """
    POSTリクエストを送信します。
    
    Args:
        url: リクエスト先URL
        data: POSTボディ（JSONで送信）
        headers: HTTPヘッダー
        timeout: タイムアウト（秒）
        
    Returns:
        JSON応答、エラー時はNone
        
    Note:
        - 通信エラー、HTTPエラー、JSONパースエラーをログに記録
        - エラーが発生した場合は Noneを返す
    """
    try:
        response = requests.post(url, json=data, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            logger.error("❌ [POST通信エラー] URL: %s | 理由: %s | レスポンス: %s", url, e, response_text)
        else:
            logger.error("❌ [POST通信エラー] URL: %s | 理由: %s", url, e)
        return None


def send_get(url, params=None, headers=None, timeout=10):
    """
    GETリクエストを送信します。
    
    Args:
        url: リクエスト先URL
        params: クエリパラメータ
        headers: HTTPヘッダー
        timeout: タイムアウト（秒）
        
    Returns:
        JSON応答、エラー時はNone
        
    Note:
        - 通信エラー、HTTPエラー、JSONパースエラーをログに記録
        - エラーが発生した場合は Noneを返す
    """
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            logger.error("❌ [GET通信エラー] URL: %s | 理由: %s | レスポンス: %s", url, e, response_text)
        else:
            logger.error("❌ [GET通信エラー] URL: %s | 理由: %s", url, e)
        return None