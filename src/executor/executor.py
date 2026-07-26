"""売買実行ラッパー雛形。

`filter_dynamic` が生成する `data/top5.json` を読み込み、
既存の `TradingBot` に注文判定を委譲する簡易ランナーです。
"""
import time
from pathlib import Path

from common.storage import read_json
from trading_bot import TradingBot
import config

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
TOP5 = DATA_DIR / 'top5.json'

def run(token: str, poll_interval: int = 5):
    bot = TradingBot(token)
    bot.initialize()

    while True:
        top5 = read_json(TOP5) or []
        for symbol in top5:
            # TradingBot.evaluate_symbol を利用して判定・発注処理を行う
            bot.evaluate_symbol(symbol)
        time.sleep(poll_interval)

if __name__ == '__main__':
    print('executor: read token from config or env and run')
