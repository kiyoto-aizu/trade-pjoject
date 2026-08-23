import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import config
from src.screening.screening import run as screening_run
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.ranking_repository import RankingRepository
from src.infrastructure.kabu.regulation_repository import RegulationRepository
from src.infrastructure.kabu.primaryexchange_repository import PrimaryExchangeRepository
from src.infrastructure.notification.line_notify import send_line_notify
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.application.screening_usecase import ScreeningUseCase


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
    data_dir = Path(__file__).resolve().parents[2] / 'data' / 'screening'
    usecase = ScreeningUseCase(
        RankingRepository(token),
        RegulationRepository(token),
        PrimaryExchangeRepository(token),
        ScreeningResultRepository(data_dir),
        send_line_notify,
    )
    screening_run(usecase)


if __name__ == '__main__':
    main()
