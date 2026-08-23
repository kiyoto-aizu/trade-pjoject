import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import config
from src.filter_dynamic.filter_dynamic import run as filtering_run
from src.application.filtering_usecase import FilteringUseCase
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.notification.line_notify import send_line_notify
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


class BoardClient:
    def __init__(self, token):
        self.token = token

    def get_current_board(self, symbol):
        return get_current_board(self.token, symbol)


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
    root = Path(__file__).resolve().parents[2] / 'data'
    usecase = FilteringUseCase(
        ScreeningResultRepository(root / 'screening'),
        BoardClient(token),
        YahooFinanceClient(),
        FilteringResultRepository(root / 'filtering'),
        send_line_notify,
    )
    filtering_run(usecase)


if __name__ == '__main__':
    main()
