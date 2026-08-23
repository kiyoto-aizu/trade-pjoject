from pathlib import Path

from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.persistence.order_history import OrderHistoryManager
from src.infrastructure.kabu.account import AccountManager
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.notification.line_notify import send_line_notify




class TradingBot(TradingUseCase):
    def __init__(self, token: str):
        order_history_path = Path(__file__).resolve().parents[2] / config.ORDER_HISTORY_FILE
        # inject infrastructure-friendly defaults
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
        # convenience helpers for quick usage
        self.order_history_manager = OrderHistoryManager(order_history_path)
        self.account_manager = AccountManager(token)
