"""
================================================================================
HTTP通信ハンドラモジュール
API通信の共通化・例外処理を一元管理します。
================================================================================
"""
import logging
import threading
import time
import requests

from src.config import config

logger = logging.getLogger(__name__)
_request_lock = threading.Lock()
_last_request_at = None


def _wait_for_request_slot() -> None:
    global _last_request_at
    with _request_lock:
        now = time.monotonic()
        if _last_request_at is not None:
            elapsed = now - _last_request_at
            wait_seconds = config.API_REQUEST_INTERVAL_SECONDS - elapsed
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        _last_request_at = time.monotonic()


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
        _wait_for_request_slot()
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
        _wait_for_request_slot()
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