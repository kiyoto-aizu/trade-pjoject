"""
================================================================================
取引実行エントリーポイント
フィルタ、リング結果を基に自動取引を実行します。
================================================================================
"""
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, time
from pathlib import Path

from src.config import config
from src.trading.trading import TradingBot
from src.infrastructure.kabu.get_token import get_api_token


def is_trading_session(now: datetime) -> bool:
    """平日の市場時間内かを判定します。"""
    if now.weekday() >= 5:
        return False
    session_start = time(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
    session_end = time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
    return session_start <= now.time() < session_end


def configure_logging() -> None:
    """ロギングを設定します。"""
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        config.LOG_FILE_PATH,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


def main(now_provider=None) -> None:
    """
    取引ボットを起動します。
    
    処理フロー：
    1. ロギングを設定
    2. APIトークンを取得
    3. TradingBotを起動し、run()メソッドを実行
    """
    configure_logging()

    now = (now_provider or datetime.now)()
    if not is_trading_session(now):
        logging.getLogger(__name__).info('市場時間外または休場日のため、取引を開始しません。')
        return

    filtering_repository = FilteringResultRepository(Path(__file__).resolve().parents[2] / 'data' / 'filtering')
    filtering_result = filtering_repository.load_for_date(now.date())
    if not filtering_result or not filtering_result.symbols:
        logging.getLogger(__name__).info('当日のフィルタ結果がないため、取引を開始しません。')
        return

    token = get_api_token()
    if not token:
        raise SystemExit('トークン取得に失敗しました。')

    bot = TradingBot(token)
    bot.run()


if __name__ == '__main__':
    main()
