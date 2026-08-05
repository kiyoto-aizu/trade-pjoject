import logging
from logging.handlers import RotatingFileHandler

import config
from trading_bot import TradingBot
from infrastructure.kabu import get_token


def configure_logging() -> None:
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

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
    logger = logging.getLogger(__name__)
    logger.info('🚀 自動発注機能搭載システムを起動しました。')

    my_token = get_token.get_api_token()
    if not my_token:
        logger.error('❌ トークン取得失敗。')
        raise SystemExit(1)

    bot = TradingBot(my_token)
    bot.run()


if __name__ == '__main__':
    main()
