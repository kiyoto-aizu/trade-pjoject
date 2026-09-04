"""
================================================================================
取引ユースケース
フィルタリング対象銘柄の売買シグナル生成・注文実行を行うアプリケーションロジック層です。
リアルタイムの価格監視、資金チェック、キルスイッチ判定などの取引制御を実装しています。
================================================================================
"""
import logging
import json
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.rules import calculate_price_limit, check_kill_switch, is_market_closed, is_safe_to_order
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
    """
    取引処理を実行するユースケッククラス。
    
    機能：
    - 株価監視と売買シグナル生成
    - 資金確認と保有株確認
    - キルスイッチによるリスク制御
    - 注文実行と履歴管理
    - 市場終了時のレポート送信
    """
    
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
        """
        TradingUseCaseを初期化します。
        
        Args:
            token: Kabu.com Station API認証トークン
            order_history_path: 注文履歴を保存するファイルパス
            market_data_client: Yahoo FinanceなどのMarketDataクライアント（オプション、テスト用）
            board_client: リアルタイム板情報クライアント（オプション、テスト用）
            wallet_client: 口座資金情報クライアント（オプション、テスト用）
            positions_client: 保有株情報クライアント（オプション、テスト用）
            order_sender: 注文送信クライアント（オプション、テスト用）
            filtering_result_repository: フィルタリング結果リポジトリ（オプション）
            notifier: 通知機能（デフォルト：LINE通知）
        """
        self.token = token
        self.order_history_path = order_history_path
        self.order_history: List[OrderHistoryEntry] = []
        # 依存性注入（テスト時は別実装を注入可能）
        self.market_data_client = market_data_client
        self.board_client = board_client
        self.wallet_client = wallet_client
        self.positions_client = positions_client
        self.order_sender = order_sender
        self.filtering_result_repository = filtering_result_repository
        self.notifier = notifier or send_line_notify
        self.last_positions = []
        self.kill_switch_triggered = False
        self.api_soft_limit: Optional[float] = None

    # ================================================================================
    # 注文履歴の管理
    # ================================================================================

    def _load_order_history(self) -> None:
        """注文履歴ファイルから過去の注文を読み込みます。"""
        if not self.order_history_path.exists():
            data = []
        else:
            try:
                data = json.loads(self.order_history_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"注文履歴ファイルの読み込みに失敗しました: {exc}") from exc
        self.order_history = [OrderHistoryEntry.from_dict(item) for item in data]

    def _save_order_history(self) -> None:
        """現在の注文履歴をファイルに保存します。"""
        write_json(self.order_history_path, [entry.to_dict() for entry in self.order_history])

    def _register_order(self, signal: TradeSignal, limit: PriceLimit, order_response: Optional[dict]) -> None:
        """
        実行した注文を履歴に記録します。
        
        Args:
            signal: 実行した取引シグナル
            limit: 発注根拠となった価格基準値
            order_response: kabu APIの発注応答（監査ログ用）
        """
        self.order_history.append(signal.to_order_history_entry(limit, order_response))
        self._save_order_history()

    # ================================================================================
    # 口座状態の取得
    # ================================================================================

    def _load_account_state(self) -> tuple[Optional[float], List[dict]]:
        """
        現在の口座状態（資金・保有株）を取得します。
        
        Returns:
            タプル: (買付可能額, 保有株リスト)
        """
        # テスト用の注入クライアント、またはデフォルトのインフラストラクチャ実装を使用
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
        """
        指定銘柄の保有株があるかどうかを確認します。
        
        Args:
            symbol: 銘柄シンボル
            positions: 保有株リスト
            
        Returns:
            保有している場合True、していない場合False
        """
        return any(
            pos.get('Symbol') == symbol and pos.get('Side') == config.OrderSide.SELL.value and int(pos.get('HoldQty', 0) or 0) > 0
            for pos in positions
        )

    # ================================================================================
    # レポート送信
    # ================================================================================

    def _send_end_of_day_report(self) -> None:
        """市場終了時に本日の取引レポートを送信します。"""
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

    # ================================================================================
    # メイン取引ループ
    # ================================================================================

    def run(self, top_symbols_path: Path | None = None, now_provider=None, sleep=None) -> None:
        """
        取引ボットを起動します。市場終了まで銘柄を監視し、売買シグナルで自動注文を実行します。
        
        処理フロー：
        1. 注文履歴を読み込み
        2. フィルタ結果または指定ファイルから監視銘柄を取得
        3. 市場終了まで以下を繰り返す：
           - 各銘柄の株価を確認
           - 売買シグナルを生成
           - 資金確認とキルスイッチ判定を実施
           - 安全が確認できた場合のみ注文実行
        4. 最終的な評価損益を確認
        5. レポートを送信
        
        Args:
            top_symbols_path: 監視銘柄リストファイルのパス（オプション）
            now_provider: 現在時刻を取得する関数（テスト用、デフォルト：datetime.now）
            sleep: スリープ関数（テスト用、デフォルト：time.sleep）
        """
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
        # 市場終了時刻まで取引ループを実行
        while not kill_switch_triggered and not is_market_closed(now_provider().time(), config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE):
            for symbol in symbols:
                # 過去5日の終値を取得
                closes = self.market_data_client.get_yahoo_5d_closes(symbol) if self.market_data_client else get_yahoo_5d_closes(symbol)
                limit = calculate_price_limit(closes)
                if limit is None:
                    continue
                # リアルタイム株価を取得
                board = self.board_client.get_current_board(self.token, symbol) if self.board_client else get_current_board(self.token, symbol)
                if not board or board.get('current_price') is None:
                    continue
                # 売買シグナルを生成
                signal = TradeSignal.evaluate(symbol, board['current_price'], limit)
                if not signal:
                    continue
                # 口座状態を確認
                wallet_amount, positions = self._load_account_state()
                self.last_positions = positions
                api_limit = get_api_soft_limit(self.token)
                if api_limit is None:
                    logger.warning("API発注上限を取得できないため、発注を停止しました。")
                    self.kill_switch_triggered = True
                    kill_switch_triggered = True
                    break
                self.api_soft_limit = api_limit
                # キルスイッチ判定
                daily_pnl = sum(float(position.get('ProfitLoss', 0) or 0) for position in positions)
                daily_orders = sum(
                    1 for entry in self.order_history
                    if entry.timestamp.startswith(now_provider().date().isoformat())
                )
                if not check_kill_switch(daily_orders, daily_pnl, config.OPERATING_CAPITAL, config, signal.price * signal.qty, api_soft_limit=self.api_soft_limit):
                    logger.warning("キルスイッチにより発注を停止しました。")
                    kill_switch_triggered = True
                    break
                # 注文の安全性を確認
                if not is_safe_to_order(signal, wallet_amount, self._has_holdings(symbol, positions), self.order_history, config.ORDER_LOCK_SECONDS):
                    continue
                # 注文を実行（成否はResultコードで判定。失敗時はNoneが返る）
                order_result = self.order_sender.place_market_order(self.token, symbol, signal.side.value) if self.order_sender else place_market_order(self.token, symbol, signal.side.value)
                if order_result and order_result.get('Result') == 0:
                    self._register_order(signal, limit, order_result)
            sleep(config.LOOP_INTERVAL)

        # 最終的な評価損益を取得して報告
        if self.positions_client:
            self.last_positions = self.positions_client.get_positions(self.token) or []
        else:
            self.last_positions = get_positions(self.token) or []
        self._send_end_of_day_report()
