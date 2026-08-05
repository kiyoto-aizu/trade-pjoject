import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import config
from infrastructure.market_data import get_5d_closes
from infrastructure.kabu import get_board
from infrastructure.kabu import get_wallet
from infrastructure.kabu import get_positions
from infrastructure.notification import line_notify
from infrastructure.kabu import send_order

logger = logging.getLogger(__name__)


class OrderHistoryManager:
    """重複発注確認と終了レポート用のローカル注文履歴を管理するクラス。"""

    def __init__(self, path: Path):
        self.path = path
        self.orders: List[Dict] = []
        self.load()

    def load(self) -> None:
        """ディスクから保存済みの注文履歴を読み込み、メモリに格納する。"""
        if self.path.exists():
            try:
                with self.path.open('r', encoding='utf-8') as f:
                    self.orders = json.load(f)
            except Exception as exc:
                raise ValueError(f"注文履歴ファイルの読み込みに失敗しました: {exc}") from exc
        else:
            self.orders = []

    def save(self) -> None:
        """メモリ上の注文履歴をディスクに保存する。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('w', encoding='utf-8') as f:
                json.dump(self.orders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("注文履歴保存失敗: %s", e)

    def has_ordered_today(self, symbol: str, side: str) -> bool:
        """同じ銘柄と売買区分の注文が本日すでにあるかどうかを判定する。"""
        today = datetime.now().strftime('%Y-%m-%d')
        return any(
            entry.get('date') == today and entry.get('symbol') == symbol and entry.get('side') == side
            for entry in self.orders
        )

    def is_recent_order(self, symbol: str, side: str, lock_seconds: int) -> bool:
        """同一注文が指定時間内に発生しているかどうかを判定する。"""
        cutoff = datetime.now() - timedelta(seconds=lock_seconds)
        for entry in reversed(self.orders):
            if entry.get('symbol') == symbol and entry.get('side') == side:
                try:
                    ts = datetime.fromisoformat(entry.get('timestamp'))
                    if ts >= cutoff:
                        return True
                except Exception:
                    continue
        return False

    def register_order(self, symbol: str, side: str, price: float, qty: int) -> None:
        """新しい注文を履歴に登録し、保存する。"""
        entry = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'timestamp': datetime.now().isoformat(),
            'symbol': symbol,
            'side': side,
            'price': price,
            'qty': qty
        }
        self.orders.append(entry)
        self.save()

    def todays_orders(self) -> List[Dict]:
        """本日実行された注文をすべて返す。"""
        today = datetime.now().strftime('%Y-%m-%d')
        return [entry for entry in self.orders if entry.get('date') == today]


class AccountManager:
    """kabu API から口座資産とポジション情報を取得・保持するクラス。"""

    def __init__(self, token: str):
        self.token = token
        self.wallet: Dict[str, Optional[float]] = {}
        self.positions: List[Dict] = []

    def refresh(self) -> None:
        """口座残高と保有ポジションを両方更新する。"""
        self._refresh_wallet()
        self._refresh_positions()

    def _refresh_wallet(self) -> None:
        """kabu API から口座のウォレット残高を取得する。"""
        wallet = get_wallet.get_wallet_cash(self.token)
        if wallet:
            self.wallet = {
                'StockAccountWallet': wallet.get('StockAccountWallet'),
                'AuKCStockAccountWallet': wallet.get('AuKCStockAccountWallet'),
                'AuJbnStockAccountWallet': wallet.get('AuJbnStockAccountWallet')
            }
            logger.info("現物買付可能額: %s", self.wallet['StockAccountWallet'])
            logger.info("三菱UFJ現物可能額: %s", self.wallet['AuKCStockAccountWallet'])
            logger.info("auじぶん銀行残高: %s", self.wallet['AuJbnStockAccountWallet'])
        else:
            logger.warning("口座資産情報の取得に失敗しました。")

    def _refresh_positions(self) -> None:
        """kabu API から現在の保有ポジションを取得する。"""
        positions = get_positions.get_positions(self.token)
        if positions is not None:
            self.positions = positions
            if self.positions:
                logger.info("保有ポジション件数: %d", len(self.positions))
            else:
                logger.info("保有ポジションはありません。")
        else:
            logger.warning("保有ポジション情報の取得に失敗しました。")

    def has_holdings(self, symbol: str) -> bool:
        """指定銘柄について保有株があるかどうかを判定する。"""
        total_qty = 0
        for pos in self.positions:
            if pos.get('Symbol') == symbol and pos.get('Side') == config.OrderSide.SELL.value:
                total_qty += int(pos.get('HoldQty', 0) or 0)
        return total_qty > 0


class AiLimitManager:
    """取引ロジックで使う価格閾値を準備・提供するクラス。"""

    def __init__(self):
        self.limits: Dict[str, Dict[str, float]] = {}

    def build(self, token: str, symbols: List[str]) -> None:
        """過去データから各銘柄の売買閾値を計算する。"""
        self.limits = {}
        for symbol in symbols:
            closes = get_5d_closes.get_yahoo_5d_closes(symbol)
            if not closes or len(closes) < 5:
                logger.warning("%s の過去データが不足しているためスキップします", symbol)
                continue
            moving_average = sum(closes) / len(closes)
            self.limits[symbol] = {
                'ma_price': round(moving_average, 1),
                'buy': round(moving_average * 0.99, 1),
                'sell': round(moving_average * 1.01, 1)
            }
            logger.info(
                "%s を記憶 -> 5日平均: %s円 (買い: %s円 / 売り: %s円)",
                symbol,
                self.limits[symbol]['ma_price'],
                self.limits[symbol]['buy'],
                self.limits[symbol]['sell'],
            )
            time.sleep(0.5)

    def get(self, symbol: str) -> Optional[Dict[str, float]]:
        """指定銘柄の計算済み閾値を返す。"""
        return self.limits.get(symbol)


class NotificationService:
    """終了レポート送信のための通知チャンネルを抽象化するクラス。"""

    @staticmethod
    def send_line_report(message: str) -> bool:
        """LINE Message API を使ってレポートメッセージを送信する。"""
        return line_notify.send_line_notify(message)


class TradingBot:
    """補助クラスを使ってメインの取引フローを実行する。"""

    ORDER_HISTORY_PATH = Path(__file__).resolve().parent / config.ORDER_HISTORY_FILE

    def __init__(self, token: str):
        self.token = token
        self.order_history = OrderHistoryManager(self.ORDER_HISTORY_PATH)
        self.account = AccountManager(token)
        self.ai_limits = AiLimitManager()

    def initialize(self) -> None:
        """注文履歴、口座情報、AI の閾値を初期化する。"""
        logger.info("起動処理: 口座資産・保有株状況の確認を開始します...")
        self.order_history.load()
        self.account.refresh()
        logger.info("口座状態確認が完了しました。")

        logger.info("起動処理: 過去データを取得・記憶中...")
        self.ai_limits.build(self.token, config.TARGET_SYMBOLS)
        logger.info("初期化完了。記憶件数: %d 件", len(self.ai_limits.limits))

    def is_market_closed(self, now: Optional[datetime] = None) -> bool:
        """現在時刻が取引終了時刻を過ぎているかどうかを判定する。"""
        if now is None:
            now = datetime.now()
        close_time = now.replace(
            hour=config.MARKET_CLOSE_HOUR,
            minute=config.MARKET_CLOSE_MINUTE,
            second=0,
            microsecond=0
        )
        return now >= close_time

    def send_end_of_day_report(self) -> None:
        """終了レポートを組み立て、通知サービス経由で送信する。"""
        today_orders = self.order_history.todays_orders()
        lines = [
            f"本日の自動売買レポート ({datetime.now().strftime('%Y-%m-%d')})",
            f"監視銘柄: {', '.join(config.TARGET_SYMBOLS)}",
            f"発注件数: {len(today_orders)}",
            f"現物買付可能額: {self.account.wallet.get('StockAccountWallet')}",
            f"保有ポジション件数: {len(self.account.positions)}"
        ]
        if today_orders:
            lines.append("--- 注文履歴 ---")
            for order in today_orders:
                side_text = '買い' if order.get('side') == config.OrderSide.BUY.value else '売り'
                lines.append(f"{side_text} {order.get('symbol')} {order.get('qty')}株 @ {order.get('price'):.1f}円")
        else:
            lines.append("本日実行された注文はありませんでした。")

        message = "\n".join(lines)
        success = NotificationService.send_line_report(message)
        if success:
            logger.info("終了レポートをLINEに送信しました。")
        else:
            logger.warning("終了レポートのLINE送信に失敗しました。")

    def check_order_safety(self, symbol: str, side: str, current_price: float) -> bool:
        """発注前に安全性チェックを行い、注文可否を判定する。"""
        if side == config.OrderSide.BUY:
            if self.account.wallet.get('StockAccountWallet') is None:
                logger.warning("予算確認未実施: 現物買付可能額が不明です。")
                return False

            required = current_price * config.DEFAULT_ORDER_QTY
            wallet_amount = self.account.wallet.get('StockAccountWallet', 0)
            if required > wallet_amount:
                logger.warning("予算不足: 注文必要額 %s 円 > 買付可能額 %s 円", required, wallet_amount)
                return False

        if self.order_history.has_ordered_today(symbol, side):
            logger.warning("当日同一シンボル・同一方向の注文が既にあります。")
            return False

        if self.order_history.is_recent_order(symbol, side, config.ORDER_LOCK_SECONDS):
            logger.warning("同一注文が %s 秒以内に発生しています。二重発注を防止します。", config.ORDER_LOCK_SECONDS)
            return False

        if side == config.OrderSide.SELL and not self.account.has_holdings(symbol):
            logger.warning("%s の保有株が確認できないため、売り注文を見送ります。", symbol)
            return False

        return True

    def evaluate_symbol(self, symbol: str) -> None:
        """銘柄を評価し、買い/売り/様子見を判定して注文する。"""
        limits = self.ai_limits.get(symbol)
        if not limits:
            return

        board_data = get_board.get_current_board(self.token, symbol)
        if not board_data:
            logger.error("%s の板情報取得に失敗しました。注文を見送ります。", symbol)
            return

        symbol_name = board_data["symbol_name"]
        current_price = board_data["current_price"]
        if current_price is None:
            logger.error("%s (%s) の現在値が取得できませんでした。注文を見送ります。", symbol_name, symbol)
            return

        time_label = "📡検証用リアルタイム"
        logger.info("%s (%s) | 現在値: %s 円 [%s]", symbol_name, symbol, current_price, time_label)
        logger.info("記憶データ: 5日平均=%s 円, buy=%s 円以下, sell=%s 円以上", limits['ma_price'], limits['buy'], limits['sell'])

        if current_price <= limits["buy"]:
            logger.info("買いシグナル発生: %s <= %s", current_price, limits["buy"])
            if self.check_order_safety(symbol, config.OrderSide.BUY, current_price):
                order_id = send_order.place_market_order(self.token, symbol, side=config.OrderSide.BUY)
                if order_id:
                    self.order_history.register_order(symbol, config.OrderSide.BUY.value, current_price, config.DEFAULT_ORDER_QTY)

        elif current_price >= limits["sell"]:
            logger.info("売りシグナル発生: %s >= %s", current_price, limits["sell"])
            if self.check_order_safety(symbol, config.OrderSide.SELL, current_price):
                order_id = send_order.place_market_order(self.token, symbol, side=config.OrderSide.SELL)
                if order_id:
                    self.order_history.register_order(symbol, config.OrderSide.SELL.value, current_price, config.DEFAULT_ORDER_QTY)
        else:
            logger.info("様子見: %s は買い/売り基準の間です。", current_price)

    def run(self) -> None:
        """取引終了までメインの監視ループを実行する。"""
        self.initialize()

        while True:
            now = datetime.now()
            logger.info("--- 自動監視チェック %s ---", now.strftime('%H:%M:%S'))
            if self.is_market_closed(now):
                logger.info("取引終了時間に達しました。終了レポートを送信します。")
                self.send_end_of_day_report()
                break

            logger.info("ループ開始前に口座・ポジションを更新します...")
            self.account.refresh()

            for symbol in config.TARGET_SYMBOLS:
                self.evaluate_symbol(symbol)
                time.sleep(1)

            logger.info("%s 秒間待機します...", config.LOOP_INTERVAL)
            time.sleep(config.LOOP_INTERVAL)
