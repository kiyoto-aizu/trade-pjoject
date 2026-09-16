"""
================================================================================
取引ユースケース
フィルタリング対象銘柄の売買シグナル生成・注文実行を行うアプリケーションロジック層です。
リアルタイムの価格監視、資金チェック、キルスイッチ判定などの取引制御を実装しています。
================================================================================
"""
import logging
import json
import inspect
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.config import config
from src.domain.models import OrderHistoryEntry, PriceLimit, TradeSignal
from src.domain.rules import calculate_buy_quantity, calculate_price_limit, calculate_rsi, check_kill_switch, is_buy_order_amount_allowed, is_market_closed, is_safe_to_order, is_trading_session
from src.domain.volatility import DailyBar, VolatilityLevel, adjust_quantity_for_volatility, assess_volatility, stop_loss_multiplier
from src.domain.market_regime import MarketRegime, resolve_rsi_entry_threshold
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_positions import get_positions
from src.infrastructure.kabu.get_wallet import get_wallet_cash
from src.infrastructure.kabu.get_apisoftlimit import get_api_soft_limit
from src.infrastructure.kabu.send_order import place_market_order
from src.infrastructure.market_data.get_daily_closes import get_yahoo_daily_bars, get_yahoo_daily_closes
from src.infrastructure.notification.slack_notify import notify_critical, notify_daily
from src.infrastructure.persistence.storage import read_json, write_json
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer

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
        daily_analyzer=None,
        daily_report_directory: Optional[Path] = None,
        market_regime_usecase=None,
        filter_decision_repository: FilterDecisionRepository | None = None,
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
            notifier: 通知機能（テスト用オプション）
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
        self.notifier = notifier
        self.daily_analyzer = daily_analyzer if daily_analyzer is not None else create_daily_analyzer()
        self.daily_report_directory = daily_report_directory or Path(__file__).resolve().parents[2] / "data" / "reports"
        self.filter_decision_repository = filter_decision_repository or FilterDecisionRepository(
            order_history_path.parent / "filter_decision_events.sqlite3"
        )
        self.market_regime_usecase = market_regime_usecase
        self.market_regime = MarketRegime.NORMAL
        self.market_regime_assessment = None
        self.last_positions = []
        self.kill_switch_triggered = False
        self.emergency_stop_triggered = False
        self.api_soft_limit: Optional[float] = None
        self._missing_holding_warning_symbols: set[str] = set()
        self._sell_condition_observations: dict[str, str] = {}
        self._liquidation_results: list[dict] = []

    @property
    def _execution_mode(self) -> str:
        return config.TRADING_MODE

    def _record_filter_decision_safely(
        self,
        event_type: str,
        symbol: str,
        occurred_at: datetime,
        reference_price: float,
        quantity: int,
        inputs: dict,
    ) -> int | None:
        try:
            return self.filter_decision_repository.record_event(
                event_type, symbol, occurred_at, reference_price, quantity, inputs, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの保存に失敗しました: 種別=%s 銘柄=%s", event_type, symbol)
            return None

    def _update_filter_decision_observations_safely(
        self, observed_at: datetime, prices_by_symbol: dict[str, float]
    ) -> None:
        try:
            self.filter_decision_repository.update_open_event_observations(
                observed_at, prices_by_symbol, self._execution_mode
            )
        except Exception:
            logger.exception("判定イベントの観測更新に失敗しました")

    def _finalize_filter_decisions_safely(self, as_of: datetime) -> None:
        try:
            self.filter_decision_repository.finalize_due_events(
                as_of, config.FILTER_DECISION_OBSERVATION_DAYS
            )
        except Exception:
            logger.exception("判定イベントの確定に失敗しました")

    def _market_regime_event_inputs(self, rsi, entry_threshold: float, assessment=None) -> dict:
        market_assessment = self.market_regime_assessment
        return {
            "atr": getattr(assessment, "atr", None),
            "true_range": getattr(assessment, "latest_true_range", None),
            "atr_ratio": getattr(assessment, "ratio", None),
            "atr_level": getattr(getattr(assessment, "level", None), "value", None),
            "market_regime": self.market_regime.value,
            "realized_volatility_percent": getattr(market_assessment, "realized_volatility_percent", None),
            "vix": getattr(market_assessment, "vix", None),
            "nikkei_change_percent": getattr(market_assessment, "nikkei_change_percent", None),
            "adx": getattr(market_assessment, "adx", None),
            "rsi": rsi,
            "rsi_normal_threshold": config.RSI_ENTRY_THRESHOLD,
            "rsi_applied_threshold": entry_threshold,
        }

    def _filter_decision_summaries(self, event_type: str) -> list[dict]:
        try:
            return [
                item for item in self.filter_decision_repository.load_summaries(
                    execution_mode=self._execution_mode
                )
                if item["event_type"] == event_type
            ]
        except Exception:
            logger.exception("判定イベントのサマリー取得に失敗しました: 種別=%s", event_type)
            return []

    def _notify_safely(self, message: str) -> None:
        """通知失敗で取引制御自体を妨げないように通知します。"""
        if self.notifier:
            try:
                self.notifier(message)
            except Exception:
                logger.exception("緊急通知の送信に失敗しました")
            return
        notify_critical(message)

    def _trigger_kill_switch(self, reason: str) -> None:
        """キルスイッチを一度だけ発動し、即時通知します。"""
        if self.kill_switch_triggered:
            return
        self.kill_switch_triggered = True
        logger.warning("キルスイッチを発動しました: %s", reason)
        self._notify_safely(f"【緊急停止】キルスイッチを発動しました: {reason}")

    def _is_emergency_stop_requested(self) -> bool:
        return config.EMERGENCY_STOP_FILE.exists()

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

    def prepare_market_regime(self):
        """取引開始前に市場レジームを取得し、通知・レポートで再利用します。"""
        if self.market_regime_usecase is not None and self.market_regime_assessment is None:
            self.market_regime_assessment = self.market_regime_usecase.execute()
            self.market_regime = self.market_regime_assessment.regime
        return self.market_regime_assessment

    def market_conditions_summary(self) -> str:
        """通知向けに市場状況を短く整形します。"""
        assessment = self.market_regime_assessment
        if assessment is None or not getattr(assessment, "data_available", False):
            reason = getattr(assessment, "failure_reason", None) or "データ未取得"
            return f"MarketRegime={self.market_regime.value} | 判定データなし: {reason}"

        def format_metric(value, suffix=""):
            return f"{value:.2f}{suffix}" if value is not None else "-"

        return (
            f"MarketRegime={self.market_regime.value} | "
            f"日経前日比={format_metric(getattr(assessment, 'nikkei_change_percent', None), '%')} | "
            f"実現ボラ={format_metric(getattr(assessment, 'realized_volatility_percent', None), '%')} | "
            f"VIX={format_metric(getattr(assessment, 'vix', None))} | "
            f"ADX={format_metric(getattr(assessment, 'adx', None))}"
        )

    def market_conditions_detail(self) -> list[str]:
        """市場指標を意味と現在の状態付きで通知用に整形します。"""
        assessment = self.market_regime_assessment
        if assessment is None or not getattr(assessment, "data_available", False):
            reason = getattr(assessment, "failure_reason", None) or "データ未取得"
            return [f"MarketRegime: {self.market_regime.value}", f"現在の状態: 判定不能（{reason}）"]

        realized_volatility = getattr(assessment, "realized_volatility_percent", None)
        vix = getattr(assessment, "vix", None)
        nikkei_change = getattr(assessment, "nikkei_change_percent", None)
        adx = getattr(assessment, "adx", None)
        if any(value is None for value in (realized_volatility, vix, nikkei_change, adx)):
            return [self.market_conditions_summary(), "補足: 一部の市場指標を取得できませんでした。"]
        thresholds = config.MARKET_REGIME_THRESHOLDS
        lines = [
            "MarketRegime: " + self.market_regime.value,
            "意味: 市場全体の警戒度です。",
            "現在の状態: " + {
                MarketRegime.NORMAL: "通常",
                MarketRegime.CAUTION: "やや警戒",
                MarketRegime.DANGER: "危険・新規買い停止",
            }[self.market_regime],
            f"日経前日比: {nikkei_change:.2f}%（意味: 日経平均の前日からの変化率 / 状態: {'上昇' if nikkei_change >= 0 else '下落'}）",
            f"実現ボラティリティ: {realized_volatility:.2f}%（意味: 市場全体の値動きの大きさ / 状態: {'危険' if realized_volatility >= thresholds.realized_vol_danger else '注意' if realized_volatility >= thresholds.realized_vol_caution else '通常'}）",
            f"VIX: {vix:.2f}（意味: 市場の不安心理 / 状態: {'危険' if vix >= thresholds.vix_danger else '注意' if vix >= thresholds.vix_caution else '通常'}）",
            f"ADX: {adx:.2f}（意味: トレンドの強さ / 状態: {'強いトレンド' if adx >= config.MARKET_REGIME_ADX_TREND_THRESHOLD else '強いトレンドなし'}）",
        ]
        return lines

    @staticmethod
    def _order_detail_lines(entry: OrderHistoryEntry, include_market_regime: bool = True) -> list[str]:
        """注文履歴を判断理由と用語補足付きの通知行へ変換します。"""
        lines = [
            f"銘柄: {entry.symbol}",
            f"売買: {'買い' if entry.side == config.OrderSide.BUY else '売り'}",
            f"約定価格: {entry.price:.1f}円",
            f"数量: {entry.qty}株",
            f"判断理由: {entry.decision_reason or '注文条件成立'}",
        ]
        if entry.rsi is not None:
            if entry.side == config.OrderSide.BUY and entry.rsi_entry_threshold is not None:
                state = "買い基準以上" if entry.rsi >= entry.rsi_entry_threshold else "買い基準未満"
            elif entry.side == config.OrderSide.SELL and entry.rsi_exit_threshold is not None:
                state = "決済基準以下" if entry.rsi <= entry.rsi_exit_threshold else "決済基準超"
            else:
                state = "判定材料"
            lines.append(f"RSI: {entry.rsi:.2f}（意味: 値動きの勢い / 状態: {state}）")
        else:
            lines.append("RSI: データなし")
        if entry.rsi_entry_threshold is not None:
            lines.append(f"RSI買い基準: {entry.rsi_entry_threshold:.2f}")
        if entry.rsi_exit_threshold is not None:
            lines.append(f"RSI決済基準: {entry.rsi_exit_threshold:.2f}")
        if entry.current_price is not None:
            lines.append(f"現在価格: {entry.current_price:.1f}円")
        if entry.basis_lower_band is not None and entry.basis_upper_band is not None:
            lines.append(
                f"価格基準: 下側バンド={entry.basis_lower_band:.1f}円 / "
                f"上側バンド={entry.basis_upper_band:.1f}円"
            )
        if include_market_regime and entry.market_regime:
            regime_state = {
                "NORMAL": "通常",
                "CAUTION": "やや警戒",
                "DANGER": "危険",
            }.get(entry.market_regime, "不明")
            lines.append(f"MarketRegime: {entry.market_regime}（意味: 市場全体の警戒度 / 状態: {regime_state}）")
        if entry.atr is not None:
            atr_ratio = entry.atr_ratio or 0.0
            atr_state = "危険" if atr_ratio >= config.ATR_DANGER_RATIO else "注意" if atr_ratio >= config.ATR_CAUTION_RATIO else "通常"
            lines.extend([
                f"ATR: {entry.atr:.3f}円（意味: 通常の値動き幅 / 状態: {atr_state}）",
                f"TR: {entry.atr_true_range:.3f}円（意味: 直近の実際の値幅）",
                f"ATR比率: {atr_ratio:.2f}（意味: 直近値幅÷ATR / 状態: {'通常より大きい' if atr_ratio >= 1 else '通常より小さい'}）",
                f"ATRレベル: {entry.atr_level}",
            ])
            if entry.atr_stop_multiplier is not None:
                lines.append(f"ATR損切り倍率: {entry.atr_stop_multiplier:.2f}倍")
        else:
            lines.append("ATR: データなし")
        if entry.order_qty_before_atr is not None:
            lines.append(f"ATR数量調整: {entry.order_qty_before_atr}株 → {entry.qty}株")
        return lines

    def _register_order(
        self,
        signal: TradeSignal,
        limit: PriceLimit,
        order_response: Optional[dict],
        volatility_assessment=None,
        diagnostics: Optional[dict] = None,
    ) -> None:
        """
        実行した注文を履歴に記録します。
        
        Args:
            signal: 実行した取引シグナル
            limit: 発注根拠となった価格基準値
            order_response: kabu APIの発注応答（監査ログ用）
        """
        order_diagnostics = {
            "market_regime": self.market_regime.value,
        }
        order_diagnostics.update(diagnostics or {})
        if volatility_assessment is not None:
            order_diagnostics.update({
                "atr": volatility_assessment.atr,
                "atr_true_range": volatility_assessment.latest_true_range,
                "atr_ratio": volatility_assessment.ratio,
                "atr_level": volatility_assessment.level.value,
                "atr_stop_multiplier": stop_loss_multiplier(
                    volatility_assessment.level,
                    config.ATR_STOP_NORMAL_MULTIPLIER,
                    config.ATR_STOP_CAUTION_MULTIPLIER,
                    config.ATR_STOP_DANGER_MULTIPLIER,
                ),
            })
        self.order_history.append(signal.to_order_history_entry(limit, order_response, order_diagnostics))
        self._save_order_history()
        side_label = "買い" if signal.side == config.OrderSide.BUY else "売り"
        entry = self.order_history[-1]
        message_lines = [
            "【業務】取引運用",
            "【機能】注文執行",
            "【概要】",
            f"{side_label}注文が成立しました。",
            "【詳細】",
            *self._order_detail_lines(entry),
        ]
        message = "\n".join(message_lines)
        if self.notifier:
            try:
                self.notifier(message)
            except Exception:
                logger.exception("注文約定通知の送信に失敗しました: 銘柄=%s", signal.symbol)
            return
        notify_critical(message)

    def _calculate_daily_pnl(self, positions: List[dict]) -> float:
        """実現損益と保有中の評価損益を合算します。"""
        unrealized_pnl = sum(float(position.get('ProfitLoss', 0) or 0) for position in positions)
        realized_pnl = 0.0
        if self.order_sender and hasattr(self.order_sender, 'get_daily_realized_pnl'):
            realized_pnl = float(self.order_sender.get_daily_realized_pnl() or 0)
        else:
            realized_pnl = sum(
                float(
                    position.get(
                        'RealizedProfitLoss',
                        position.get('RealizedPnL', position.get('RealizedProfitLossAmount', 0)),
                    )
                    or 0
                )
                for position in positions
            )
        return unrealized_pnl + realized_pnl

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
        elif self.order_sender and hasattr(self.order_sender, 'get_wallet_cash'):
            wallet = self.order_sender.get_wallet_cash(self.token)
        else:
            wallet = get_wallet_cash(self.token)

        if self.positions_client:
            positions = self.positions_client.get_positions(self.token) or []
        elif self.order_sender and hasattr(self.order_sender, 'get_positions'):
            positions = self.order_sender.get_positions(self.token) or []
        else:
            positions = get_positions(self.token) or []
        wallet_amount = None
        if wallet is not None:
            wallet_amount = wallet.get('StockAccountWallet')
        return wallet_amount, positions

    @staticmethod
    def _is_open_position(position: dict) -> bool:
        """信用売り建て決済待ちのポジションかどうかを判定する。"""
        return (
            position.get('Side') == config.OrderSide.SELL.value
            and int(position.get('HoldQty', 0) or 0) > 0
        )

    def _count_open_positions(self, positions: List[dict]) -> int:
        """信用売り建て決済待ち＝保有中とみなすポジション数を数える。"""
        return sum(self._is_open_position(position) for position in positions)

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
            pos.get('Symbol') == symbol and self._is_open_position(pos)
            for pos in positions
        )

    def _get_daily_bars(self, symbol: str) -> list[DailyBar] | None:
        if self.market_data_client:
            getter = getattr(self.market_data_client, 'get_yahoo_daily_bars', None)
            return getter(symbol) if getter else None
        return get_yahoo_daily_bars(symbol)

    @staticmethod
    def _get_position_entry_price(position: dict) -> float | None:
        """証券会社・ペーパー実装の平均取得価格フィールドを吸収します。"""
        for key in ('AveragePrice', 'AvgPrice', 'EntryPrice'):
            value = position.get(key)
            if value is not None and float(value) > 0:
                return float(value)
        return None

    def collect_preflight_market_data(self, symbols: list[str]) -> dict[str, dict] | None:
        """初回の売買判断に必要な確定終値と板価格を取得します。"""
        market_data = {}
        for symbol in symbols:
            daily_bars = self._get_daily_bars(symbol)
            closes = [bar.close for bar in daily_bars] if daily_bars is not None else (
                self.market_data_client.get_yahoo_daily_closes(symbol)
                if self.market_data_client else get_yahoo_daily_closes(symbol)
            )
            if not closes or len(closes) < config.RSI_MINIMUM_CLOSES:
                return None
            board = (
                self.board_client.get_current_board(self.token, symbol)
                if self.board_client else get_current_board(self.token, symbol)
            )
            if not board or board.get('current_price') is None:
                return None
            snapshot = {'closes': closes, 'board': board}
            if daily_bars is not None:
                snapshot['daily_bars'] = daily_bars
            market_data[symbol] = snapshot
        return market_data

    # ================================================================================
    # レポート送信
    # ================================================================================

    def _daily_log_error_summary(self, today: str) -> dict:
        """当日のログからエラーの有無と代表的な概要だけを集計します。"""
        patterns = (" ERROR ", "異常終了", "予期しないエラー")
        messages = []
        for path in Path(config.LOG_DIRECTORY).glob("trade_project.log*"):
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.startswith(today) or not any(pattern in line for pattern in patterns):
                        continue
                    try:
                        logged_at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if is_trading_session(
                        logged_at,
                        config.MARKET_OPEN_HOUR,
                        config.MARKET_OPEN_MINUTE,
                        config.MARKET_CLOSE_HOUR,
                        config.MARKET_CLOSE_MINUTE,
                    ):
                        messages.append(line.split(": ", 1)[-1].strip())
            except OSError:
                logger.warning("日次ログを読み込めません: %s", path)
        counts = Counter(messages)
        return {
            "has_errors": bool(messages),
            "count": len(messages),
            "summaries": [message for message, _ in counts.most_common(5)],
        }

    def _record_atr_danger_skip(self, symbol: str, observed_at: datetime, price: float, assessment, quantity: int) -> None:
        """ATR DANGERで見送った買いシグナルと、その後の観測価格を記録します。"""
        self._record_filter_decision_safely("ATR_DANGER_SKIP", symbol, observed_at, price, quantity, {
            "atr": assessment.atr,
            "true_range": assessment.latest_true_range,
            "atr_ratio": assessment.ratio,
            "atr_level": getattr(getattr(assessment, "level", None), "value", None),
        })

    def _update_atr_danger_skip_observation(self, symbol: str, observed_at: datetime, price: float) -> None:
        self._update_filter_decision_observations_safely(observed_at, {symbol: price})

    def _atr_danger_skip_summary(self) -> list[dict]:
        summaries = self._filter_decision_summaries("ATR_DANGER_SKIP")
        for item in summaries:
            item["skipped_at"] = item["occurred_at"]
            item["entry_price"] = item["reference_price"]
        return summaries

    def _record_atr_stop_exit(
        self,
        symbol: str,
        sold_at: datetime,
        entry_price: float,
        exit_price: float,
        quantity: int,
        assessment,
        stop_multiplier: float,
    ) -> None:
        """ATR損切りで約定した売却と、その後の価格推移を記録します。"""
        self._record_filter_decision_safely("ATR_STOP_EXIT", symbol, sold_at, exit_price, quantity, {
            "entry_price": entry_price,
            "atr": assessment.atr,
            "atr_ratio": assessment.ratio,
            "atr_level": assessment.level.value,
            "stop_multiplier": stop_multiplier,
            "stop_price": entry_price - assessment.atr * stop_multiplier,
            "realized_pnl_before_cost": (exit_price - entry_price) * quantity,
        })

    def _update_atr_stop_exit_observation(self, symbol: str, observed_at: datetime, price: float) -> None:
        self._update_filter_decision_observations_safely(observed_at, {symbol: price})

    def _atr_stop_exit_summary(self) -> list[dict]:
        summaries = self._filter_decision_summaries("ATR_STOP_EXIT")
        for item in summaries:
            item["sold_at"] = item["occurred_at"]
            item["exit_price"] = item["reference_price"]
            item["entry_price"] = item["inputs"].get("entry_price")
            item["stop_price"] = item["inputs"].get("stop_price")
            item["realized_pnl_before_cost"] = item["inputs"].get("realized_pnl_before_cost")
            item["lowest_price_after_exit"] = item["lowest_price"]
            item["highest_price_after_exit"] = item["highest_price"]
            item["last_price_after_exit"] = item["last_price"]
            item["post_exit_change_percent"] = item["price_change_percent"]
            item["avoided_pnl_before_cost"] = item["hypothetical_pnl_before_cost"]
        return summaries

    def _send_end_of_day_report(self) -> None:
        """市場終了時に本日の取引レポートを送信します。"""
        self._load_order_history()
        today = datetime.now().date().isoformat()
        log_error_summary = self._daily_log_error_summary(today)
        daily_orders = [entry for entry in self.order_history if entry.timestamp.startswith(today)]
        self._finalize_filter_decisions_safely(datetime.now())
        atr_danger_skips = self._atr_danger_skip_summary()
        atr_stop_exits = self._atr_stop_exit_summary()
        market_regime_danger_skips = self._filter_decision_summaries("MARKET_REGIME_DANGER_SKIP")
        market_regime_caution_rsi_filters = self._filter_decision_summaries("MARKET_REGIME_CAUTION_RSI_FILTER")
        adx_trend_reliefs = self._filter_decision_summaries("ADX_TREND_RELIEF")
        lines = [
            "【業務】取引運用",
            "【機能】取引終了",
            "【概要】",
            f"{config.TRADING_MODE_LABEL}の本日の取引を終了しました。",
            "【詳細】",
            f"発注件数: {len(daily_orders)}",
        ]
        if self.kill_switch_triggered:
            lines.append("キルスイッチ: 発動")
        if self.emergency_stop_triggered:
            lines.append("手動緊急停止: 発動")
        if daily_orders:
            lines.append("注文履歴:")
            for entry in daily_orders:
                lines.extend(self._order_detail_lines(entry, include_market_regime=False))
                lines.append("")
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
        lines.append("--- エラー概要 ---")
        if log_error_summary["has_errors"]:
            lines.append(f"エラーあり: {log_error_summary['count']}件")
            lines.extend(f"- {summary}" for summary in log_error_summary["summaries"])
        else:
            lines.append("エラーなし")
        if atr_danger_skips:
            lines.append("ATR DANGER見送り:")
            for item in atr_danger_skips:
                lines.append(
                    f"{item['symbol']}: {item['outcome']} "
                    f"({item['hypothetical_pnl_before_cost']:+.0f}円概算)"
                )
        if atr_stop_exits:
            lines.append("ATR損切り売却:")
            for item in atr_stop_exits:
                lines.append(
                    f"{item['symbol']}: {item['outcome']} "
                    f"({item['avoided_pnl_before_cost']:+.0f}円概算)"
                )
        if self._liquidation_results:
            lines.append("--- 強制売却確認 ---")
            lines.extend(
                f"{item['symbol']}: {item['status']}"
                + (f" ({item['reason']})" if item.get("reason") else "")
                for item in self._liquidation_results
            )

        daily_summary = {
            "date": today,
            "trading_mode": config.TRADING_MODE_LABEL,
            "order_count": len(daily_orders),
            "orders": [
                {
                    "symbol": entry.symbol,
                    "side": entry.side.value,
                    "price": entry.price,
                    "qty": entry.qty,
                    "result_code": entry.result_code,
                    "atr": entry.atr,
                    "atr_true_range": entry.atr_true_range,
                    "atr_ratio": entry.atr_ratio,
                    "atr_level": entry.atr_level,
                    "atr_stop_multiplier": entry.atr_stop_multiplier,
                    "market_regime": entry.market_regime,
                }
                for entry in daily_orders
            ],
            "market_conditions": {
                "regime": self.market_regime.value,
                "realized_volatility_percent": getattr(self.market_regime_assessment, "realized_volatility_percent", None),
                "vix": getattr(self.market_regime_assessment, "vix", None),
                "nikkei_change_percent": getattr(self.market_regime_assessment, "nikkei_change_percent", None),
                "adx": getattr(self.market_regime_assessment, "adx", None),
                "data_available": getattr(self.market_regime_assessment, "data_available", False),
                "failure_reason": getattr(self.market_regime_assessment, "failure_reason", None),
            },
            "positions": [
                {
                    "symbol": position.get("Symbol", ""),
                    "profit_loss": float(position.get("ProfitLoss", 0) or 0),
                    "profit_loss_rate": float(position.get("ProfitLossRate", 0) or 0),
                }
                for position in self.last_positions
            ],
            "total_profit_loss": sum(float(position.get("ProfitLoss", 0) or 0) for position in self.last_positions),
            "kill_switch_triggered": self.kill_switch_triggered,
            "emergency_stop_triggered": self.emergency_stop_triggered,
            "log_errors": log_error_summary,
            "atr_danger_skips": atr_danger_skips,
            "atr_stop_exits": atr_stop_exits,
            "market_regime_danger_skips": market_regime_danger_skips,
            "market_regime_caution_rsi_filters": market_regime_caution_rsi_filters,
            "adx_trend_reliefs": adx_trend_reliefs,
            "liquidation_results": self._liquidation_results,
        }
        analysis = None
        if self.daily_analyzer:
            analysis = self.daily_analyzer.analyze(daily_summary)
            if analysis:
                lines.extend(["--- LLM日次評価（参考） ---", analysis])

        report_text = "\n".join(lines)
        report_data = {
            **daily_summary,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "report_text": report_text,
            "llm_analysis": analysis if self.daily_analyzer else None,
        }
        self.daily_report_directory.mkdir(parents=True, exist_ok=True)
        write_json(self.daily_report_directory / f"{today}.json", report_data)
        if self.notifier:
            self.notifier(report_text)
        else:
            notify_daily(report_text)

    def _liquidate_all_positions(self) -> None:
        """持ち越しを防ぐため、現物の全保有を成行で売却します。"""
        self._liquidation_results = []
        _, positions = self._load_account_state()
        self.last_positions = positions
        holdings: dict[str, int] = {}
        for position in positions:
            if position.get('Side') != config.OrderSide.SELL.value:
                continue
            quantity = int(position.get('HoldQty', 0) or 0)
            if quantity > 0:
                symbol = str(position.get('Symbol', ''))
                if symbol:
                    holdings[symbol] = holdings.get(symbol, 0) + quantity

        for symbol, quantity in holdings.items():
            try:
                board = (
                    self.board_client.get_current_board(self.token, symbol)
                    if self.board_client else get_current_board(self.token, symbol)
                )
                price = float((board or {}).get('current_price') or 0)
                signal = TradeSignal(symbol, config.OrderSide.SELL, price, quantity)
                if self.order_sender and hasattr(self.order_sender, 'set_price') and price > 0:
                    self.order_sender.set_price(symbol, price)
                order_method = self.order_sender.place_market_order if self.order_sender else place_market_order
                order_args = (self.token, symbol, config.OrderSide.SELL.value, quantity)
                try:
                    inspect.signature(order_method).bind(*order_args)
                except TypeError:
                    order_args = order_args[:3]
                order_result = order_method(*order_args)
                if order_result and order_result.get('Result') == 0:
                    self._register_order(
                        signal,
                        PriceLimit(price, price),
                        order_result,
                        diagnostics={"decision_reason": "持ち越し防止"},
                    )
                    self._liquidation_results.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "売却注文受付",
                        "reason": self._sell_condition_observations.get(
                            symbol, "通常SELL条件の観測なし（15:20以降の起動など）"
                        ),
                    })
                    logger.info("持ち越し防止売りを発注しました: 銘柄=%s | 数量=%s", symbol, quantity)
                else:
                    self._liquidation_results.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "売却注文失敗",
                        "reason": self._sell_condition_observations.get(
                            symbol, "通常SELL条件の観測なし（15:20以降の起動など）"
                        ),
                    })
                    logger.error("持ち越し防止売りに失敗しました: 銘柄=%s | 数量=%s", symbol, quantity)
            except Exception:
                self._liquidation_results.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "status": "売却処理エラー",
                    "reason": self._sell_condition_observations.get(
                        symbol, "通常SELL条件の観測なし（15:20以降の起動など）"
                    ),
                })
                logger.exception("持ち越し防止売り中にエラーが発生しました: 銘柄=%s", symbol)

    # ================================================================================
    # メイン取引ループ
    # ================================================================================

    def run(self, top_symbols_path: Path | None = None, now_provider=None, sleep=None, preflight_market_data=None) -> None:
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
        self._missing_holding_warning_symbols.clear()
        now_provider = now_provider or datetime.now
        sleep = sleep or time.sleep
        try:
            open_events = self.filter_decision_repository.load_open_events(self._execution_mode)
            logger.info("継続観測中の判定イベント: %d件", len(open_events))
        except Exception:
            logger.exception("判定イベントの継続観測初期化に失敗しました")
        if self.filtering_result_repository:
            result = self.filtering_result_repository.load_latest()
            today = now_provider().date().isoformat()
            if not result or result.date != today or not result.symbols:
                message = "当日のフィルタ結果がないため、取引を開始しません"
                if self.notifier:
                    self.notifier(message)
                else:
                    notify_daily(message)
                return
            symbols = result.symbols
        else:
            symbols = read_json(top_symbols_path) if top_symbols_path else []
            symbols = symbols or []
        if not symbols:
            logger.info("上位銘柄リストが空です。取引を行いません。")
            return

        if self.market_regime_usecase is not None:
            self.prepare_market_regime()
            logger.info(
                "MarketRegimeを取得しました: レジーム=%s | データ取得=%s | 理由=%s",
                self.market_regime.value,
                self.market_regime_assessment.data_available,
                self.market_regime_assessment.failure_reason or "なし",
            )

        kill_switch_triggered = False
        filter_decisions_initialized = False
        # 市場終了時刻まで取引ループを実行
        use_preflight_market_data = bool(preflight_market_data)
        while not kill_switch_triggered:
            if self._is_emergency_stop_requested():
                self.emergency_stop_triggered = True
                self._trigger_kill_switch("手動緊急停止フラグが検知されました")
                self._liquidate_all_positions()
                break
            now = now_provider()
            if not filter_decisions_initialized:
                self._finalize_filter_decisions_safely(now)
                filter_decisions_initialized = True
            if is_market_closed(now.time(), config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE):
                break
            if not config.ALLOW_OVERNIGHT_HOLDING and is_market_closed(
                now.time(),
                config.MARKET_LIQUIDATION_HOUR,
                config.MARKET_LIQUIDATION_MINUTE,
            ):
                self._liquidate_all_positions()
                break
            for symbol in symbols:
                try:
                    # RSI計算用の確定日足終値を取得
                    snapshot = preflight_market_data.get(symbol) if use_preflight_market_data else None
                    if snapshot:
                        closes = snapshot['closes']
                    elif self.market_data_client:
                        closes = self.market_data_client.get_yahoo_daily_closes(symbol)
                    else:
                        closes = get_yahoo_daily_closes(symbol)
                    limit = calculate_price_limit(closes)
                    rsi = calculate_rsi(closes, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
                    if limit is None:
                        continue
                    # リアルタイム株価を取得
                    board = snapshot['board'] if snapshot else (
                        self.board_client.get_current_board(self.token, symbol)
                        if self.board_client else get_current_board(self.token, symbol)
                    )
                    if not board or board.get('current_price') is None:
                        continue
                    self._update_filter_decision_observations_safely(
                        now, {symbol: float(board['current_price'])}
                    )
                    daily_bars = snapshot.get('daily_bars') if snapshot else self._get_daily_bars(symbol)
                    assessment = assess_volatility(
                        daily_bars,
                        config.ATR_PERIOD,
                        config.ATR_CAUTION_RATIO,
                        config.ATR_DANGER_RATIO,
                    ) if daily_bars else None
                    entry_threshold = resolve_rsi_entry_threshold(
                        self.market_regime,
                        config.RSI_ENTRY_THRESHOLD,
                        config.RSI_ENTRY_THRESHOLD_CAUTION,
                    )
                    entry_price = None
                    atr_stop_multiplier = None
                    decision_reason = None
                    # 売買シグナルを生成
                    signal = TradeSignal.evaluate(
                        symbol,
                        board['current_price'],
                        limit,
                        rsi,
                        entry_threshold,
                        config.RSI_EXIT_THRESHOLD,
                    )
                    if signal is not None:
                        decision_reason = (
                            "上側バンド突破・RSI条件成立"
                            if signal.side == config.OrderSide.BUY
                            else "下側バンド到達・RSI条件成立"
                        )
                    if signal is None and assessment is not None:
                        _, current_positions = self._load_account_state()
                        position = next(
                            (
                                position for position in current_positions
                                if position.get('Symbol') == symbol
                                and position.get('Side') == config.OrderSide.SELL.value
                                and int(position.get('HoldQty', 0) or 0) > 0
                            ),
                            None,
                        )
                        entry_price = self._get_position_entry_price(position) if position else None
                        if entry_price is not None:
                            atr_stop_multiplier = stop_loss_multiplier(
                                assessment.level,
                                config.ATR_STOP_NORMAL_MULTIPLIER,
                                config.ATR_STOP_CAUTION_MULTIPLIER,
                                config.ATR_STOP_DANGER_MULTIPLIER,
                            )
                            signal = TradeSignal.evaluate(
                                symbol,
                                board['current_price'],
                                limit,
                                rsi,
                                entry_threshold,
                                config.RSI_EXIT_THRESHOLD,
                                entry_price,
                                assessment.atr,
                                atr_stop_multiplier,
                            )
                            if signal is not None:
                                decision_reason = "ATR損切り基準到達"
                    if signal is None:
                        _, observed_positions = self._load_account_state()
                        held_position = next(
                            (
                                position for position in observed_positions
                                if position.get("Symbol") == symbol
                                and position.get("Side") == config.OrderSide.SELL.value
                                and int(position.get("HoldQty", 0) or 0) > 0
                            ),
                            None,
                        )
                        if held_position:
                            price_reason = (
                                f"現在値={board['current_price']:.1f} > 決済基準={limit.lower_band:.1f}"
                                if board['current_price'] > limit.lower_band
                                else "現在値条件は成立したがRSI条件未成立"
                            )
                            rsi_reason = (
                                f"RSI={rsi:.1f} > 決済RSI基準={config.RSI_EXIT_THRESHOLD:.1f}"
                                if rsi is not None and rsi > config.RSI_EXIT_THRESHOLD
                                else "RSIが取得できない"
                            )
                            self._sell_condition_observations[symbol] = (
                                f"通常SELL条件未成立: {price_reason}; {rsi_reason}"
                            )
                        if self.market_regime == MarketRegime.CAUTION:
                            normal_signal = TradeSignal.evaluate(
                                symbol, board['current_price'], limit, rsi,
                                config.RSI_ENTRY_THRESHOLD, config.RSI_EXIT_THRESHOLD,
                            )
                            if normal_signal is not None and normal_signal.side == config.OrderSide.BUY:
                                wallet_amount, _ = self._load_account_state()
                                if wallet_amount is not None:
                                    estimated_budget = min(
                                        wallet_amount / config.TARGET_POSITIONS,
                                        config.MAX_ORDER_AMOUNT_PER_TRADE,
                                    )
                                    estimated_quantity = calculate_buy_quantity(
                                        normal_signal.price, estimated_budget, config.ORDER_UNIT
                                    )
                                    if estimated_quantity > 0:
                                        self._record_filter_decision_safely(
                                            "MARKET_REGIME_CAUTION_RSI_FILTER", symbol, now,
                                            float(board['current_price']), estimated_quantity,
                                            self._market_regime_event_inputs(rsi, entry_threshold),
                                        )
                    logger.info(
                        "売買判定: 銘柄=%s | 現在値=%.1f | エントリー基準=%.1f | 決済基準=%.1f | RSI=%.1f | エントリーRSI基準=%.1f | 決済RSI基準=%.1f | 判定=%s",
                        symbol,
                        board['current_price'],
                        limit.lower_band,
                        limit.upper_band,
                        rsi,
                        entry_threshold,
                        config.RSI_EXIT_THRESHOLD,
                        signal.side.name if signal else "なし",
                    )
                    if not signal:
                        continue
                    # 口座状態を確認
                    wallet_amount, positions = self._load_account_state()
                    self.last_positions = positions
                    api_limit = config.API_SOFT_LIMIT if config.IS_DEMO else get_api_soft_limit(self.token)
                    if api_limit is None:
                        logger.warning("API発注上限を取得できないため、発注を停止しました。")
                        self._trigger_kill_switch("API発注上限を取得できません")
                        kill_switch_triggered = True
                        break
                    self.api_soft_limit = api_limit
                    if signal.side == config.OrderSide.BUY:
                        has_holdings = self._has_holdings(symbol, positions)
                        open_position_count = self._count_open_positions(positions)
                        if not has_holdings and open_position_count >= config.TARGET_POSITIONS:
                            logger.info(
                                "保有上限のため新規買いを見送ります: 銘柄=%s | 保有数=%s/%s",
                                symbol,
                                open_position_count,
                                config.TARGET_POSITIONS,
                            )
                            continue
                        if wallet_amount is None:
                            logger.warning("現物買付可能額が不明なため、買い注文を見送ります。")
                            continue
                        original_qty = 0
                        budget_per_position = min(
                            wallet_amount / config.TARGET_POSITIONS,
                            config.MAX_ORDER_AMOUNT_PER_TRADE,
                            self.api_soft_limit,
                        )
                        signal.qty = calculate_buy_quantity(
                            signal.price,
                            budget_per_position,
                            config.ORDER_UNIT,
                        )
                        if daily_bars:
                            if assessment:
                                original_qty = signal.qty
                                signal.qty = adjust_quantity_for_volatility(
                                    signal.qty,
                                    assessment.level,
                                    config.ORDER_UNIT,
                                    config.ATR_CAUTION_LOT_RATIO,
                                    config.ATR_DANGER_ACTION,
                                )
                                logger.info(
                                    "ATR数量調整: 銘柄=%s | ATR=%.3f | TR=%.3f | 倍率=%.2f | レベル=%s | 数量=%s->%s",
                                    symbol,
                                    assessment.atr,
                                    assessment.latest_true_range,
                                    assessment.ratio,
                                    assessment.level.value,
                                    original_qty,
                                    signal.qty,
                                )
                                if (
                                    assessment.level == VolatilityLevel.DANGER
                                    and config.ATR_DANGER_ACTION == "skip"
                                    and signal.qty == 0
                                    and original_qty > 0
                                ):
                                    self._record_atr_danger_skip(
                                        symbol,
                                        now,
                                        float(board['current_price']),
                                        assessment,
                                        original_qty,
                                    )
                        if self.market_regime == MarketRegime.DANGER:
                            if signal.qty > 0:
                                self._record_filter_decision_safely(
                                    "MARKET_REGIME_DANGER_SKIP", symbol, now,
                                    float(board['current_price']), signal.qty,
                                    self._market_regime_event_inputs(rsi, entry_threshold, assessment),
                                )
                            logger.info(
                                "MarketRegimeにより新規買いを見送ります: 銘柄=%s | レジーム=%s",
                                symbol,
                                self.market_regime.value,
                            )
                            continue
                        elif self.market_regime == MarketRegime.CAUTION:
                            logger.info(
                                "MarketRegimeによりエントリーRSI基準を引き上げました: 銘柄=%s | レジーム=%s | RSI基準=%.1f",
                                symbol,
                                self.market_regime.value,
                                entry_threshold,
                            )
                    else:
                        held_quantity = next(
                            (
                                int(position.get('HoldQty', 0) or 0)
                                for position in positions
                                if position.get('Symbol') == symbol
                                and position.get('Side') == config.OrderSide.SELL.value
                            ),
                            0,
                        )
                        signal.qty = held_quantity if held_quantity > 0 else config.ORDER_UNIT
                        original_qty = signal.qty
                    if signal.qty <= 0:
                        logger.info("注文数量が0のため見送ります: 銘柄=%s", symbol)
                        continue
                    # キルスイッチ判定
                    daily_pnl = self._calculate_daily_pnl(positions)
                    daily_orders = sum(
                        1 for entry in self.order_history
                        if entry.timestamp.startswith(now_provider().date().isoformat())
                    )
                    if not check_kill_switch(daily_orders, daily_pnl, config.OPERATING_CAPITAL, config):
                        logger.warning("キルスイッチにより発注を停止しました。")
                        self._trigger_kill_switch(
                            f"日次損益または発注回数の上限超過（損益={daily_pnl:.1f}円、発注件数={daily_orders}件）"
                        )
                        kill_switch_triggered = True
                        break
                    if (
                        signal.side == config.OrderSide.BUY
                        and not is_buy_order_amount_allowed(
                            signal.price * signal.qty,
                            config,
                            api_soft_limit=self.api_soft_limit,
                        )
                    ):
                        logger.warning(
                            "買い注文を見送ります: 銘柄=%s | 注文金額=%.1f円 | 上限=%.1f円",
                            symbol,
                            signal.price * signal.qty,
                            min(config.MAX_ORDER_AMOUNT_PER_TRADE, self.api_soft_limit),
                        )
                        continue
                    # 注文の安全性を確認
                    has_holdings = self._has_holdings(symbol, positions)
                    should_warn_missing_holdings = (
                        signal.side == config.OrderSide.SELL
                        and not has_holdings
                        and symbol not in self._missing_holding_warning_symbols
                    )
                    if not is_safe_to_order(
                        signal,
                        wallet_amount,
                        has_holdings,
                        self.order_history,
                        config.ORDER_LOCK_SECONDS,
                        warn_on_missing_holdings=should_warn_missing_holdings,
                    ):
                        if signal.side == config.OrderSide.SELL and not has_holdings:
                            self._missing_holding_warning_symbols.add(symbol)
                        continue
                    # 注文を実行（成否はResultコードで判定。失敗時はNoneが返る）
                    if self.order_sender and hasattr(self.order_sender, 'set_price'):
                        self.order_sender.set_price(symbol, signal.price)
                    order_method = (
                        self.order_sender.place_market_order
                        if self.order_sender
                        else place_market_order
                    )
                    order_args = (self.token, symbol, signal.side.value, signal.qty)
                    try:
                        inspect.signature(order_method).bind(*order_args)
                    except TypeError:
                        order_args = order_args[:3]
                    order_result = order_method(*order_args)
                    if order_result and order_result.get('Result') == 0:
                        self._register_order(
                            signal,
                            limit,
                            order_result,
                            assessment,
                            diagnostics={
                                "decision_reason": decision_reason,
                                "rsi": rsi,
                                "rsi_entry_threshold": entry_threshold,
                                "rsi_exit_threshold": config.RSI_EXIT_THRESHOLD,
                                "current_price": board['current_price'],
                                "order_qty_before_atr": original_qty,
                            },
                        )
                        if decision_reason == "ATR損切り基準到達":
                            self._record_atr_stop_exit(
                                symbol,
                                now,
                                entry_price,
                                float(signal.price),
                                signal.qty,
                                assessment,
                                atr_stop_multiplier,
                            )
                        if (
                            signal.side == config.OrderSide.BUY
                            and getattr(self.market_regime_assessment, "trend_relief_applied", False)
                        ):
                            self._record_filter_decision_safely(
                                "ADX_TREND_RELIEF", symbol, now, float(signal.price), signal.qty,
                                self._market_regime_event_inputs(rsi, entry_threshold, assessment),
                            )
                        logger.info(
                            "%s成立: 銘柄=%s | 約定価格=%.1f | 数量=%s | 注文受付番号=%s",
                            "買い" if signal.side == config.OrderSide.BUY else "売り",
                            symbol,
                            signal.price,
                            signal.qty,
                            order_result.get('OrderId'),
                        )
                except Exception:
                    # 想定外の例外は当該銘柄のみスキップし、ループ全体を止めない
                    logger.exception("%s の評価中に予期しないエラーが発生しました", symbol)
                    continue
            use_preflight_market_data = False
            sleep(config.LOOP_INTERVAL)

        # 最終的な評価損益を取得して報告
        if self.positions_client:
            self.last_positions = self.positions_client.get_positions(self.token) or []
        elif self.order_sender and hasattr(self.order_sender, 'get_positions'):
            self.last_positions = self.order_sender.get_positions(self.token) or []
        else:
            self.last_positions = get_positions(self.token) or []
        self._send_end_of_day_report()
