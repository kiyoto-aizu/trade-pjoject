import logging
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.rules import calculate_price_limit, check_kill_switch, is_safe_to_order
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_positions import get_positions
from src.infrastructure.kabu.get_wallet import get_wallet_cash
from src.infrastructure.kabu.get_apisoftlimit import get_api_soft_limit
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
        filtering_result_repository=None,
        notifier=None,
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
        self.filtering_result_repository = filtering_result_repository
        self.notifier = notifier or send_line_notify
        self.last_positions = []
        self.kill_switch_triggered = False

    def _load_order_history(self) -> None:
        if not self.order_history_path.exists():
            data = []
        else:
            try:
                data = json.loads(self.order_history_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"注文履歴ファイルの読み込みに失敗しました: {exc}") from exc
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
        today = datetime.now().date().isoformat()
        daily_orders = [entry for entry in self.order_history if entry.timestamp.startswith(today)]
        lines = [
            f"本日の自動売買レポート ({config.ORDER_HISTORY_FILE})",
            f"発注件数: {len(daily_orders)}"
        ]
        if self.kill_switch_triggered:
            lines.append("キルスイッチ: 発動")
        if daily_orders:
            lines.append("--- 注文履歴 ---")
            for entry in daily_orders:
                lines.append(f"{entry.symbol} {entry.side.value} {entry.qty}株 @ {entry.price:.1f}円")
        else:
            lines.append("本日実行された注文はありませんでした。")
        if self.last_positions:
            lines.append("--- 評価損益 ---")
            for position in self.last_positions:
                lines.append(
                    f"{position.get('Symbol', '')}: "
                    f"{position.get('ProfitLoss', 0)}円 "
                    f"({position.get('ProfitLossRate', 0)}%)"
                )
            total_pnl = sum(float(position.get('ProfitLoss', 0) or 0) for position in self.last_positions)
            lines.append(f"合計損益: {total_pnl}円")

        self.notifier("\n".join(lines))

    def run(self, top_symbols_path: Path | None = None, now_provider=None, sleep=None) -> None:
        self._load_order_history()
        now_provider = now_provider or datetime.now
        sleep = sleep or time.sleep
        if self.filtering_result_repository:
            result = self.filtering_result_repository.load_latest()
            today = now_provider().date().isoformat()
            if not result or result.date != today or not result.symbols:
                self.notifier("当日のフィルタ結果がないため、取引を開始しません")
                return
            symbols = result.symbols
        else:
            symbols = read_json(top_symbols_path) if top_symbols_path else []
            symbols = symbols or []
        if not symbols:
            logger.info("上位銘柄リストが空です。取引を行いません。")
            return

        kill_switch_triggered = False
        while not kill_switch_triggered and (now_provider().hour < config.MARKET_CLOSE_HOUR or (now_provider().hour == config.MARKET_CLOSE_HOUR and now_provider().minute < config.MARKET_CLOSE_MINUTE)):
            for symbol in symbols:
                closes = self.market_data_client.get_yahoo_5d_closes(symbol) if self.market_data_client else get_yahoo_5d_closes(symbol)
                limit = calculate_price_limit(closes)
                if limit is None:
                    continue
                board = self.board_client.get_current_board(self.token, symbol) if self.board_client else get_current_board(self.token, symbol)
                if not board or board.get('current_price') is None:
                    continue
                signal = TradeSignal.evaluate(symbol, board['current_price'], limit)
                if not signal:
                    continue
                wallet_amount, positions = self._load_account_state()
                self.last_positions = positions
                api_limit = get_api_soft_limit(self.token)
                if api_limit is None:
                    logger.warning("API発注上限を取得できないため、発注を停止しました。")
                    self.kill_switch_triggered = True
                    kill_switch_triggered = True
                    break
                config.API_SOFT_LIMIT = api_limit
                daily_pnl = sum(float(position.get('ProfitLoss', 0) or 0) for position in positions)
                daily_orders = sum(
                    1 for entry in self.order_history
                    if entry.timestamp.startswith(now_provider().date().isoformat())
                )
                if not check_kill_switch(daily_orders, daily_pnl, config.OPERATING_CAPITAL, config, signal.price * signal.qty):
                    logger.warning("キルスイッチにより発注を停止しました。")
                    kill_switch_triggered = True
                    break
                if not is_safe_to_order(signal, wallet_amount, self._has_holdings(symbol, positions), self.order_history, config.ORDER_LOCK_SECONDS):
                    continue
                order_result = self.order_sender.place_market_order(self.token, symbol, signal.side.value) if self.order_sender else place_market_order(self.token, symbol, signal.side.value)
                if order_result:
                    self._register_order(signal)
            sleep(config.LOOP_INTERVAL)

        if self.positions_client:
            self.last_positions = self.positions_client.get_positions(self.token) or []
        else:
            self.last_positions = get_positions(self.token) or []
        self._send_end_of_day_report()
