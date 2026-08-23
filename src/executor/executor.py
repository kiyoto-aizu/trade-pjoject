"""
================================================================================
取引実行ラッパー
フィルタリング結果を監視し、TradingBotに注文判定を委譲する実行層です。

機能：
- フィルタ結果ファイル（data/top5.json）を定期的に読み込み
- 各銘柄について注文判定を実施
- TradingBotに処理を委譲
================================================================================
"""
import logging
import time
from pathlib import Path

from src.infrastructure.persistence.storage import read_json
from src.trading.trading import TradingBot
from src.config.config import config

logger = logging.getLogger(__name__)

# データディレクトリとトップ5銘柄ファイルのパス定義
DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
TOP5 = DATA_DIR / 'top5.json'


def run(token: str, poll_interval: int = 5):
    """
    取引ボットを実行します。
    
    処理フロー：
    1. トークンを使用してTradingBotをインスタンス化
    2. ボットを初期化
    3. 定期的にフィルタ結果を読み込み
    4. 各銘柄について注文判定を実施
    
    Args:
        token: Kabu.com Station API認証トークン
        poll_interval: ポーリング間隔（秒、デフォルト：5秒）
        
    Note:
        このループは無限に実行され、プロセス終了まで継続します。
    """
    logger.info('executor: read token from config or env and run')
    bot = TradingBot(token)
    bot.initialize()

    while True:
        # フィルタ結果を読み込み（存在しない場合は空リスト）
        top5 = read_json(TOP5) or []
        for symbol in top5:
            # TradingBot.evaluate_symbol を利用して判定・発注処理を行う
            bot.evaluate_symbol(symbol)
        time.sleep(poll_interval)
