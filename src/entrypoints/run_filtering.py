"""
================================================================================
フィルタリング実行エントリーポイント
スクリーニング結果から出来高急騰銘柄を抽出し、次の売買対象を決定します。
================================================================================
"""
import argparse
import logging
from datetime import date
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.config import config
from src.filter_dynamic.filter_dynamic import run as filtering_run
from src.application.filtering_usecase import FilteringUseCase
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.notification.line_notify import process_notification, send_line_notify
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


class BoardClient:
    """
    リアルタイム板情報を取得するクライアントラッパー。
    テスト時に別実装を注入可能な設計になっています。
    """
    
    def __init__(self, token):
        """
        BoardClientを初期化します。
        
        Args:
            token: Kabu.com Station API認証トークン
        """
        self.token = token

    def get_current_board(self, symbol):
        """
        指定銘柄の現在の板情報を取得します。
        
        Args:
            symbol: 銘柄シンボル
            
        Returns:
            板情報を含む辞書
        """
        return get_current_board(self.token, symbol)


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
    フィルタリング処理を実行します。
    
    処理フロー：
    1. API トークンを取得
    2. スクリーニング結果リポジトリを初期化
    3. 各銘柄の本日出来高と過去平均を比較
    4. 出来高急騰率でトップ10に絞り込み
    5. 結果を保存・通知
    """
    parser = argparse.ArgumentParser(description="フィルタリングを実行します")
    parser.add_argument("--date", dest="target_date", type=date.fromisoformat, help="対象日 (YYYY-MM-DD)。指定時は日足から過去結果を再計算")
    args = parser.parse_args()

    configure_logging()
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、フィルタリングを中止します。")
            return
        with process_notification('フィルタリング', notify_lifecycle=False):
            root = Path(__file__).resolve().parents[2] / 'data'
            token = None
            board_client = None
            notifier = None
            if args.target_date is None:
                token = get_api_token()
                if not token:
                    raise SystemExit('トークン取得に失敗しました。')
                if unregister_all(token) is None:
                    raise SystemExit('銘柄登録の全解除に失敗しました。')
                board_client = BoardClient(token)
                notifier = send_line_notify
            usecase = FilteringUseCase(
                ScreeningResultRepository(root / 'screening'),
                board_client,
                YahooFinanceClient(),
                FilteringResultRepository(root / 'filtering'),
                notifier,
            )
            filtering_run(usecase, target_date=args.target_date)


if __name__ == '__main__':
    main()
