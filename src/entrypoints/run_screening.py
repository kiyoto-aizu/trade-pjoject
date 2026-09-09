"""
================================================================================
スクリーニング実行エントリーポイント
松元鉮ランキングから取引候補銀柄を技不的に選別します。
================================================================================
"""
import argparse
import logging
from datetime import date
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import config
from src.screening.screening import run as screening_run
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.ranking_repository import RankingRepository
from src.infrastructure.kabu.regulation_repository import RegulationRepository
from src.infrastructure.kabu.primaryexchange_repository import PrimaryExchangeRepository
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.kabu.register import register_symbols
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.notification.line_notify import process_notification, send_line_notify
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.persistence.listed_security_repository import ListedSecurityRepository
from src.infrastructure.persistence.historical_regulation_repository import HistoricalRegulationRepository
from src.infrastructure.market_data.historical_ranking_repository import HistoricalRankingRepository
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.application.screening_usecase import ScreeningUseCase


def configure_logging() -> None:
    """ロギングを設定します。"""
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    file_handler = TimedRotatingFileHandler(
        config.LOG_FILE_PATH,
        when='midnight',
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


def main() -> None:
    """
    スクリーニング処理を実行します。
    
    処理フロー：
    1. APIトークンを取得
    2. 各リポジトリを害化
    3. ScreeningUseCaseを実行
    """
    parser = argparse.ArgumentParser(description="スクリーニングを実行します")
    parser.add_argument("--date", dest="target_date", type=date.fromisoformat, help="対象日 (YYYY-MM-DD)。指定時は銘柄マスタと日足から再計算")
    args = parser.parse_args()

    configure_logging()
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、スクリーニングを中止します。")
            return
        with process_notification('スクリーニング', notify_lifecycle=False):
            token = get_api_token()
            if not token:
                raise SystemExit('トークン取得に失敗しました。')
            if unregister_all(token) is None:
                raise SystemExit('銘柄登録の全解除に失敗しました。')
            data_dir = Path(__file__).resolve().parents[2] / 'data' / 'screening'
            root = Path(__file__).resolve().parents[2]
            ranking_repository = RankingRepository(token)
            regulation_repository = RegulationRepository(token)
            if args.target_date:
                ranking_repository = HistoricalRankingRepository(
                    ListedSecurityRepository(root / 'data' / 'universe' / 'listed_securities.csv'),
                    YahooFinanceClient(),
                )
                regulation_repository = HistoricalRegulationRepository(
                    root / 'data' / 'regulation' / 'historical_regulations.csv'
                )
            usecase = ScreeningUseCase(
                ranking_repository,
                regulation_repository,
                PrimaryExchangeRepository(token),
                ScreeningResultRepository(data_dir),
                send_line_notify,
            )
            usecase.batch_started = lambda batch, _: register_symbols(token, batch) is not None
            usecase.batch_finished = lambda _, __: unregister_all(token) is not None
            screening_run(usecase, target_date=args.target_date)


if __name__ == '__main__':
    main()
