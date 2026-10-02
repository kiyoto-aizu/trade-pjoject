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


def _is_token_endpoint(url: str) -> bool:
    return url.rstrip('/').endswith('/token')


def _status_code(error: requests.RequestException):
    response = getattr(error, 'response', None)
    return getattr(response, 'status_code', None) if response is not None else None


def _log_request_error(url, error, error_label) -> None:
    response_text = None
    if hasattr(error, 'response') and getattr(error, 'response') is not None:
        response_text = getattr(error.response, 'text', None)
    if response_text:
        logger.error("❌ [%s] URL: %s | 理由: %s | レスポンス: %s", error_label, url, error, response_text)
    else:
        logger.error("❌ [%s] URL: %s | 理由: %s", error_label, url, error)


def _request_once(requests_func, url, timeout, **kwargs):
    response = requests_func(url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def _send_with_auth_retry(requests_func, url, timeout, error_label, **kwargs):
    """
    共通のHTTP送信処理。kabuステーションAPIが401を返した場合、共有トークン提供者で
    トークンを再取得し、同じ呼び出しを1回だけ再試行します（/token自体は対象外）。
    """
    headers = kwargs.get('headers')
    try:
        _wait_for_request_slot()
        return _request_once(requests_func, url, timeout, **kwargs)
    except requests.RequestException as e:
        if _status_code(e) == 401 and headers and 'X-API-KEY' in headers and not _is_token_endpoint(url):
            from src.infrastructure.kabu.token_provider import get_token_provider

            provider = get_token_provider()
            new_token = provider.recover_from_unauthorized()
            if new_token:
                retry_headers = dict(headers)
                retry_headers['X-API-KEY'] = new_token
                retry_kwargs = dict(kwargs, headers=retry_headers)
                try:
                    _wait_for_request_slot()
                    result = _request_once(requests_func, url, timeout, **retry_kwargs)
                    provider.report_retry_succeeded()
                    return result
                except requests.RequestException as retry_error:
                    if _status_code(retry_error) == 401:
                        provider.report_retry_still_unauthorized()
                    _log_request_error(url, retry_error, error_label)
                    return None
        _log_request_error(url, e, error_label)
        return None


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
        - 401応答の場合はトークンを再取得し1回だけ再試行します
    """
    return _send_with_auth_retry(requests.post, url, timeout, "POST通信エラー", json=data, headers=headers)


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
        - 401応答の場合はトークンを再取得し1回だけ再試行します
    """
    return _send_with_auth_retry(requests.get, url, timeout, "GET通信エラー", params=params, headers=headers)


def send_put(url, data=None, headers=None, timeout=10):
    """PUTリクエストを送信し、JSON応答を返します。401応答の場合はトークンを再取得し1回だけ再試行します。"""
    return _send_with_auth_retry(requests.put, url, timeout, "PUT通信エラー", json=data, headers=headers)