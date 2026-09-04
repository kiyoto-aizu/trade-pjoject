"""
================================================================================
取引ボット実装
TradingUseCaseを継承した、インフラストラクチャ統合版の実装です。
Kabu.com Station APIとの連携を行う主要なクラスを提供します。
================================================================================
"""
from pathlib import Path

from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.notification.line_notify import send_line_notify


class TradingBot(TradingUseCase):
    """
    実際の取引を実行するボットクラス。
    
    TradingUseCaseにインフラストラクチャレイヤーの実装を統合し、
    実際のAPI通信と永続化を実行します。
    """
    
    def __init__(self, token: str):
        """
        TradingBotを初期化します。
        
        Args:
            token: Kabu.com Station API認証トークン
        """
        order_history_path = Path(__file__).resolve().parents[2] / config.ORDER_HISTORY_FILE
        # インフラストラクチャレイヤーのデフォルト実装を注入
        super().__init__(
            token=token,
            order_history_path=order_history_path,
            market_data_client=None,
            board_client=None,
            wallet_client=None,
            positions_client=None,
            order_sender=None,
            filtering_result_repository=FilteringResultRepository(Path(__file__).resolve().parents[2] / 'data' / 'filtering'),
            notifier=send_line_notify,
        )
