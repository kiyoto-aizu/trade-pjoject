# infrastructure/kabu/get_token.py
import config
from api import request_handler

def get_api_token():
    url = f"{config.BASE_URL}/token"
    headers = {'Content-Type': 'application/json'}
    data = {"APIPassword": config.API_PASSWORD}
    
    # 💡 共通化したPOST処理を呼び出す
    res_json = request_handler.send_post(url, data=data, headers=headers)
    
    if res_json:
        return res_json.get('Token')
    return None