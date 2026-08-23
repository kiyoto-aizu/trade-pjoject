import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import config
from src.trading.trading import TradingBot
from src.infrastructure.kabu.get_token import get_api_token


def configure_logging() -> None:
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


def main() -> None:
    configure_logging()

    token = get_api_token()
    if not token:
        raise SystemExit('トークン取得に失敗しました。')

    bot = TradingBot(token)
    bot.run()


if __name__ == '__main__':
    main()
