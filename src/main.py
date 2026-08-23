"""
================================================================================
メインエントリーポイント
取引ボットシステムの起動と設定を行うモジュール。
ロギング設定とボットの初期化・実行を管理します。
================================================================================
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import config
from src.trading.trading import TradingBot
from src.infrastructure.kabu.get_token import get_api_token


# ================================================================================
# ログ設定関数
# ================================================================================

def configure_logging() -> None:
    """
    アプリケーション全体のロギング設定を初期化します。
    
    設定内容：
    - ロギングレベルをconfig.LOG_LEVELから取得
    - コンソールとファイルの両方に出力
    - ローテーション機能付きでログファイルを管理
    - ログフォーマット: タイムスタンプ、レベル、モジュール名、メッセージ
    """
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # コンソール出力ハンドラ
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    # ファイル出力ハンドラ（ローテーション機能付き）
    file_handler = RotatingFileHandler(
        config.LOG_FILE_PATH,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


# ================================================================================
# メイン処理
# ================================================================================

def main() -> None:
    """
    取引ボットを起動します。
    
    処理フロー：
    1. ロギングを初期化
    2. Kabu.com Station APIのトークンを取得
    3. トークンに基づいてTradingBotをインスタンス化
    4. トップ5銘柄データを読み込んでボットを実行
    
    Raises:
        SystemExit: トークン取得失敗時に終了コード1で終了
    """
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info('🚀 自動発注機能搭載システムを起動しました。')

    my_token = get_api_token()
    if not my_token:
        logger.error('❌ トークン取得失敗。')
        raise SystemExit(1)
    bot = TradingBot(my_token)
    top5_path = Path(__file__).resolve().parents[1] / 'data' / 'top5.json'
    bot.run(top_symbols_path=top5_path)


if __name__ == '__main__':
    main()
