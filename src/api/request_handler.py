# api/request_handler.py
import logging
import requests

logger = logging.getLogger(__name__)

def send_post(url, data=None, headers=None, timeout=10):
    """共通POSTリクエスト関数"""
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
    """共通GETリクエスト関数"""
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