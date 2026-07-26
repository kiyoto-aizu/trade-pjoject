import requests
import config

def send_line_notify(message: str) -> bool:
    token = config.LINE_MESSAGE_CHANNEL_TOKEN
    to_user = config.LINE_MESSAGE_TO
    if not token:
        print("⚠️ LINE Message API 用チャネルアクセストークンが設定されていません。config.LINE_MESSAGE_CHANNEL_TOKEN を確認してください。")
        return False
    if not to_user:
        print("⚠️ 送信先のLINEユーザーIDが設定されていません。config.LINE_MESSAGE_TO を確認してください。")
        return False

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    payload = {
        'to': to_user,
        'messages': [
            {
                'type': 'text',
                'text': message
            }
        ]
    }
    try:
        response = requests.post(config.LINE_MESSAGE_API, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            print(f"❌ LINE送信失敗: {e} | レスポンス: {response_text}")
        else:
            print(f"❌ LINE送信失敗: {e}")
        return False
