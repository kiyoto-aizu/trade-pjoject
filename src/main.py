from trading_bot import TradingBot
from component import get_token


if __name__ == '__main__':
    print('🚀 自動発注機能搭載システムを起動しました。')

    my_token = get_token.get_api_token()
    if not my_token:
        print('❌ トークン取得失敗。')
        exit()

    bot = TradingBot(my_token)
    bot.run()
