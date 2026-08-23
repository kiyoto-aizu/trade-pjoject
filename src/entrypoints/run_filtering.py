"""
================================================================================
フィルタリング実行エントリーポイント
スクリーニング結果から出来高急騰銘柄を抽出し、次の売買対象を決定します。
================================================================================
"""
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

    file_handler = RotatingFileHandler(
        config.LOG_FILE_PATH,
        maxBytes=config.LOG_MAX_BYTES,
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
