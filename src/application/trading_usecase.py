import logging
from pathlib import Path
from typing import List, Optional

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.rules import calculate_price_limit, is_safe_to_order
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_positions import get_positions
from src.infrastructure.kabu.get_wallet import get_wallet_cash
from src.infrastructure.kabu.send_order import place_market_order
from src.infrastructure.market_data.get_5d_closes import get_yahoo_5d_closes
from src.infrastructure.notification.line_notify import send_line_notify
from src.infrastructure.persistence.storage import read_json, write_json

logger = logging.getLogger(__name__)


class TradingUseCase:
    def __init__(
        self,
        token: str,
        order_history_path: Path,
        market_data_client=None,
        board_client=None,
        wallet_client=None,
        positions_client=None,
        order_sender=None,
    ):
        self.token = token
        self.order_history_path = order_history_path
        self.order_history: List[OrderHistoryEntry] = []
        # dependency injection (defaults to infrastructure implementations)
        self.market_data_client = market_data_client
        self.board_client = board_client
        self.wallet_client = wallet_client
        self.positions_client = positions_client
        self.order_sender = order_sender

    def _load_order_history(self) -> None:
        data = read_json(self.order_history_path) or []
        self.order_history = [OrderHistoryEntry.from_dict(item) for item in data]

    def _save_order_history(self) -> None:
        write_json(self.order_history_path, [entry.to_dict() for entry in self.order_history])

    def _register_order(self, signal: TradeSignal) -> None:
        self.order_history.append(signal.to_order_history_entry())
        self._save_order_history()

    def _load_account_state(self) -> tuple[Optional[float], List[dict]]:
        # use injected clients when provided (for testing), otherwise default infra
        if self.wallet_client:
            wallet = self.wallet_client.get_wallet_cash(self.token)
        else:
            wallet = get_wallet_cash(self.token)

        if self.positions_client:
            positions = self.positions_client.get_positions(self.token) or []
        else:
            positions = get_positions(self.token) or []
        wallet_amount = None
        if wallet is not None:
            wallet_amount = wallet.get('StockAccountWallet')
        return wallet_amount, positions

    def _has_holdings(self, symbol: str, positions: List[dict]) -> bool:
        return any(
            pos.get('Symbol') == symbol and pos.get('Side') == config.OrderSide.SELL.value and int(pos.get('HoldQty', 0) or 0) > 0
            for pos in positions
        )

    def _send_end_of_day_report(self) -> None:
        lines = [
            f"本日の自動売買レポート ({config.ORDER_HISTORY_FILE})",
            f"発注件数: {len(self.order_history)}"
        ]
        if self.order_history:
            lines.append("--- 注文履歴 ---")
            for entry in self.order_history:
                lines.append(f"{entry.symbol} {entry.side.value} {entry.qty}株 @ {entry.price:.1f}円")
        else:
            lines.append("本日実行された注文はありませんでした。")

        send_line_notify("\n".join(lines))

    def run(self, top_symbols_path: Path) -> None:
        self._load_order_history()
        wallet_amount, positions = self._load_account_state()
        symbols = read_json(top_symbols_path) or []
        if not symbols:
            logger.info("上位銘柄リストが空です。取引を行いません。")
            return

        for symbol in symbols:
            # calculate price limit via injected market data client or default
            closes = None
            if self.market_data_client:
                closes = self.market_data_client.get_yahoo_5d_closes(symbol)
            else:
                closes = get_yahoo_5d_closes(symbol)
            limit = calculate_price_limit(closes)
            if limit is None:
                logger.warning("%s の価格閾値を計算できませんでした。スキップします。", symbol)
                continue

            if self.board_client:
                board = self.board_client.get_current_board(self.token, symbol)
            else:
                board = get_current_board(self.token, symbol)
            if not board or board.get('current_price') is None:
                logger.warning("%s の板情報取得に失敗しました。", symbol)
                continue

            current_price = board['current_price']
            signal = TradeSignal.evaluate(symbol, current_price, limit)
            if not signal:
                logger.info("%s は閾値に達していないため、発注しません。", symbol)
                continue

            has_holdings = self._has_holdings(symbol, positions)
            if not is_safe_to_order(signal, wallet_amount, has_holdings, self.order_history, config.ORDER_LOCK_SECONDS):
                continue

            if self.order_sender:
                order_result = self.order_sender.place_market_order(self.token, symbol, signal.side.value)
            else:
                order_result = place_market_order(self.token, symbol, signal.side.value)
            if order_result:
                self._register_order(signal)
                logger.info("%s の注文を登録しました。", symbol)
            else:
                logger.warning("%s の注文送信に失敗しました。", symbol)

        self._send_end_of_day_report()
