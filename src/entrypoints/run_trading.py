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
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.market_data.get_5d_closes import get_yahoo_5d_closes
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository


def is_trading_session(now: datetime) -> bool:
    """平日の市場時間内かを判定します。"""
    if now.weekday() >= 5:
        return False
    session_start = time(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
    session_end = time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)
    return session_start <= now.time() < session_end


def collect_market_data_sources(token: str, symbols: list[str]) -> dict[str, dict] | None:
    """対象銘柄の過去終値と現在の板価格を取得し、初回判定用に返します。"""
    market_data = {}
    for symbol in symbols:
        closes = get_yahoo_5d_closes(symbol)
        if not closes or len(closes) < 5:
            return None
        board = get_current_board(token, symbol)
        if not board or board.get('current_price') is None:
            return None
        market_data[symbol] = {'closes': closes, 'board': board}
    return market_data


def check_market_data_sources(token: str, symbols: list[str]) -> bool:
    """対象銘柄の過去終値と現在の板価格を取得できるか確認します。"""
    return collect_market_data_sources(token, symbols) is not None


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

    market_data = collect_market_data_sources(token, filtering_result.symbols)
    if market_data is None:
        logging.getLogger(__name__).error('市場データまたは板情報を取得できないため、取引を開始しません。')
        return

    bot = TradingBot(token)
    bot.run(preflight_market_data=market_data)


if __name__ == '__main__':
    main()
