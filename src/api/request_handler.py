# api/request_handler.py
import requests

def send_post(url, data=None, headers=None, timeout=10):
    """共通POSTリクエスト関数"""
    try:
        response = requests.post(url, json=data, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            print(f"❌ [POST通信エラー] URL: {url} | 理由: {e} | レスポンス: {response_text}")
        else:
            print(f"❌ [POST通信エラー] URL: {url} | 理由: {e}")
        return None

def send_get(url, params=None, headers=None, timeout=10):
    """共通GETリクエスト関数"""
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            print(f"❌ [GET通信エラー] URL: {url} | 理由: {e} | レスポンス: {response_text}")
        else:
            print(f"❌ [GET通信エラー] URL: {url} | 理由: {e}")
        return None