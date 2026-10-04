"""
================================================================================
フィルタリング実行エントリーポイント
スクリーニング結果から出来高急騰銘柄を抽出し、次の売買対象を決定します。
================================================================================
"""
import argparse
import logging
from datetime import date, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from time import monotonic, perf_counter

from src.config import config
from src.application.market_regime_usecase import MarketRegimeUseCase
from src.application.filtering_usecase import FilteringDeadlineExceeded, FilteringUseCase
from src.infrastructure.persistence.decision_journal_repository import DecisionJournalRepository
from src.infrastructure.kabu.get_board import get_current_board
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.kabu.token_provider import get_token_provider
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.notification.slack_notify import notify_daily, process_notification
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.calendar.japanese_calendar import is_trading_day

def notify_result(message: str) -> None:
    notify_daily(message)


class RegistrationAwareBoardCache:
    """板取得を同日中に共有し、API登録銘柄数を50以下に保ちます。"""

    def __init__(self, board_client, unregister_callback, batch_size: int = 50):
        self.board_client = board_client
        self.unregister_callback = unregister_callback
        self.batch_size = batch_size
        self._cache: dict[str, tuple[str, object]] = {}
        self._requests_since_clear = 0
        self._blocked_error: Exception | None = None
        self.fetch_count = 0
        self.cache_hit_count = 0
        self.fetch_duration_ms = 0.0
        self.unregister_count = 0

    def clear_registrations(self) -> bool:
        self.unregister_count += 1
        try:
            result = self.unregister_callback()
        except Exception as exc:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            self._blocked_error.__cause__ = exc
            return False
        if result is None:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            return False
        self._requests_since_clear = 0
        self._blocked_error = None
        return True

    def get_current_board(self, symbol):
        key = str(symbol)
        saved = self._cache.get(key)
        if saved is not None:
            self.cache_hit_count += 1
            if saved[0] == "error":
                raise saved[1]
            return saved[1]
        if self._blocked_error is not None:
            raise self._blocked_error
        if self._requests_since_clear >= self.batch_size:
            if not self.clear_registrations():
                raise self._blocked_error

        started = perf_counter()
        self.fetch_count += 1
        try:
            board = self.board_client.get_current_board(symbol)
        except Exception as exc:
            self._cache[key] = ("error", exc)
            raise
        finally:
            self._requests_since_clear += 1
            self.fetch_duration_ms += (perf_counter() - started) * 1000
        self._cache[key] = ("result", board)
        return board


class CachedVolumeClient:
    """同一実行内の重複した20日平均取得を再利用します。"""

    def __init__(self, volume_client):
        self.volume_client = volume_client
        self._average_details: dict[tuple[str, int, str | None], dict | Exception] = {}

    def __getattr__(self, name):
        return getattr(self.volume_client, name)

    def get_average_turnover_details(self, symbol, days=20, target_date=None):
        date_key = target_date.isoformat() if target_date is not None else None
        key = (str(symbol), days, date_key)
        saved = self._average_details.get(key)
        if isinstance(saved, Exception):
            raise saved
        if saved is not None:
            return dict(saved)
        try:
            details = self.volume_client.get_average_turnover_details(symbol, days, target_date)
        except Exception as exc:
            self._average_details[key] = exc
            raise
        self._average_details[key] = dict(details)
        return dict(details)


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
    if not is_trading_day(args.target_date or date.today()):
        logging.getLogger(__name__).info('休場日のため、フィルタリングを開始しません。')
        return
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、フィルタリングを中止します。")
            return
        with process_notification(
            'フィルタリング',
            notify_lifecycle=False,
            trigger='スクリーニング結果',
        ):
            token = None
            board_client = None
            board_cache = None
            notifier = None
            if args.target_date is None:
                token = get_api_token()
                if not token:
                    raise SystemExit('トークン取得に失敗しました。')
                if unregister_all(token) is None:
                    raise SystemExit('銘柄登録の全解除に失敗しました。')
                board_cache = RegistrationAwareBoardCache(
                    BoardClient(token),
                    lambda: unregister_all(token),
                    batch_size=config.SCREENING_BATCH_SIZE,
                )
                board_client = board_cache
                notifier = notify_result
            journal_repository = None
            if args.target_date is None:
                # 過去日指定の再実行で本番の記録を汚さないよう、当日実行時のみ記録する
                try:
                    journal_repository = DecisionJournalRepository(
                        config.FILTER_DECISION_DATABASE_FILE, execution_mode=config.TRADING_MODE
                    )
                except Exception:
                    logging.getLogger(__name__).exception('判断記録DBの初期化に失敗しました。記録なしで続行します。')
            volume_client = CachedVolumeClient(YahooFinanceClient())
            usecase = FilteringUseCase(
                ScreeningResultRepository(config.SCREENING_RESULT_DIRECTORY),
                board_client,
                volume_client,
                FilteringResultRepository(config.FILTERING_RESULT_DIRECTORY),
                notifier,
                decision_journal_repository=journal_repository,
                diagnostics_repository=(
                    FilteringDiagnosticsRepository(config.FILTERING_DIAGNOSTICS_DIRECTORY)
                    if args.target_date is None else None
                ),
                        market_regime_usecase=MarketRegimeUseCase(
                            market_data_client=YahooIndexClient(),
                            thresholds=config.MARKET_REGIME_THRESHOLDS,
                            realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
                            data_range=config.MARKET_REGIME_DATA_RANGE,
                            adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
                        ),
            )
            try:
                usecase.execute(target_date=args.target_date)
            except Exception:
                if board_cache is not None and not board_cache.clear_registrations():
                    logging.getLogger(__name__).error("フィルタ失敗後の銘柄登録解除にも失敗しました。")
                raise
            if board_cache is not None:
                primary_fetch_count = board_cache.fetch_count
                primary_cache_hit_count = board_cache.cache_hit_count
                primary_fetch_duration_ms = board_cache.fetch_duration_ms
                additional_started = perf_counter()
                if not board_cache.clear_registrations():
                    logging.getLogger(__name__).error(
                        "270円フィルタ保存後の銘柄登録解除に失敗したため、追加価格帯フィルタを中止します。"
                    )
                else:
                    wall_now = datetime.now()
                    deadline_at = datetime.combine(
                        wall_now.date(), config.FILTERING_PRICE_BAND_DEADLINE_TIME
                    )
                    deadline_monotonic = monotonic() + max(
                        0.0, (deadline_at - wall_now).total_seconds()
                    )
                    for price_cap in sorted(config.SCREENING_ALTERNATE_PRICE_CAPS):
                        cap_name = f"{price_cap:g}"
                        screening_directory = config.SCREENING_PRICE_BAND_RESULT_ROOT / cap_name
                        filtering_directory = config.FILTERING_PRICE_BAND_RESULT_ROOT / cap_name
                        try:
                            alternate_usecase = FilteringUseCase(
                                ScreeningResultRepository(screening_directory),
                                board_cache,
                                volume_client,
                                FilteringResultRepository(filtering_directory),
                                diagnostics_repository=FilteringDiagnosticsRepository(
                                    filtering_directory / "diagnostics"
                                ),
                            )
                            result = alternate_usecase.execute(
                                target_date=args.target_date,
                                price_cap=price_cap,
                                deadline_monotonic=deadline_monotonic,
                                price_band=cap_name,
                            )
                            logging.getLogger(__name__).info(
                                "価格帯別フィルタ完了: 上限=%s円 | 採用=%d件 | 保存先=%s",
                                cap_name,
                                len(result.symbols),
                                filtering_directory,
                            )
                        except FilteringDeadlineExceeded as exc:
                            logging.getLogger(__name__).warning(
                                "価格帯別フィルタを締め切りで打ち切りました: 上限=%s円 | %s",
                                cap_name,
                                exc,
                            )
                        except Exception:
                            logging.getLogger(__name__).exception(
                                "価格帯別フィルタに失敗しました: 上限=%s円", cap_name
                            )
                logger = logging.getLogger(__name__)
                logger.info(
                    "価格帯別板取得サマリー: 追加ユニーク取得=%d件 | 追加キャッシュ再利用=%d件 | "
                    "板取得時間合計=%.3fms | 追加処理経過=%.3fms | 登録解除=%d回",
                    board_cache.fetch_count - primary_fetch_count,
                    board_cache.cache_hit_count - primary_cache_hit_count,
                    board_cache.fetch_duration_ms - primary_fetch_duration_ms,
                    (perf_counter() - additional_started) * 1000,
                    board_cache.unregister_count,
                )
                if not board_cache.clear_registrations():
                    logging.getLogger(__name__).error("フィルタ終了時の銘柄登録解除に失敗しました。")
            if get_token_provider().recovery_failed:
                # 1run1回: トークン再取得後も401が続いた(復旧失敗)場合のみ通知する
                notify_result("kabuステーションAPIの認証が回復しません（トークン再取得後も401が継続しました）。")


if __name__ == '__main__':
    main()
