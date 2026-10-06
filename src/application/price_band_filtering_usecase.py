import logging
from datetime import datetime, timedelta
from time import monotonic, perf_counter

from src.application.filtering_usecase import FilteringDeadlineExceeded, FilteringUseCase
from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


class PriceBandFilteringUseCase:
    def __init__(
        self,
        board_cache,
        volume_client,
        *,
        now=None,
        monotonic_clock=None,
        perf_counter_clock=None,
        filtering_use_case_factory=None,
        screening_repository_factory=None,
        filtering_repository_factory=None,
        diagnostics_repository_factory=None,
        logger=None,
    ):
        self.board_cache = board_cache
        self.volume_client = volume_client
        self.now = now or datetime.now
        self.monotonic_clock = monotonic_clock or monotonic
        self.perf_counter_clock = perf_counter_clock or perf_counter
        self.filtering_use_case_factory = filtering_use_case_factory or FilteringUseCase
        self.screening_repository_factory = screening_repository_factory or ScreeningResultRepository
        self.filtering_repository_factory = filtering_repository_factory or FilteringResultRepository
        self.diagnostics_repository_factory = diagnostics_repository_factory or FilteringDiagnosticsRepository
        self.logger = logger or logging.getLogger(__name__)

    def run(self, primary_usecase, target_date=None, board_retry=None) -> None:
        board_cache = self.board_cache
        primary_fetch_count = board_cache.fetch_count
        primary_cache_hit_count = board_cache.cache_hit_count
        primary_fetch_duration_ms = board_cache.fetch_duration_ms
        started_at = self.perf_counter_clock()
        wall_now = self.now()
        today = target_date or wall_now.date()
        previous_business_day = today - timedelta(days=1)
        while not is_trading_day(previous_business_day):
            previous_business_day -= timedelta(days=1)
        deadline_at = datetime.combine(wall_now.date(), config.FILTERING_PRICE_BAND_DEADLINE_TIME)
        deadline_monotonic = self.monotonic_clock() + max(
            0.0, (deadline_at - wall_now).total_seconds()
        )

        primary_screening = primary_usecase.screening_repository.load_for_date(previous_business_day)
        band_specs = []
        for price_cap in sorted(config.SCREENING_ALTERNATE_PRICE_CAPS):
            cap_name = f"{price_cap:g}"
            screening_directory = config.SCREENING_PRICE_BAND_RESULT_ROOT / cap_name
            screening_repository = self.screening_repository_factory(screening_directory)
            band_specs.append((
                price_cap,
                cap_name,
                screening_repository,
                screening_repository.load_for_date(previous_business_day),
                config.FILTERING_PRICE_BAND_RESULT_ROOT / cap_name,
            ))

        all_symbols = list(dict.fromkeys(
            str(symbol)
            for screening in [primary_screening, *(spec[3] for spec in band_specs)]
            if screening
            for symbol in screening.symbols
        ))
        metrics_before_getter = getattr(board_cache, "get_metrics_snapshot", None)
        metrics_before = metrics_before_getter() if callable(metrics_before_getter) else None
        prefetch_started = self.perf_counter_clock()
        fetched = board_cache.fetch_current_boards(
            all_symbols, max_workers=config.FILTER_BOARD_MAX_CONCURRENCY
        )
        prefetch_elapsed_ms = (self.perf_counter_clock() - prefetch_started) * 1000
        metrics_after_prefetch = metrics_before_getter() if callable(metrics_before_getter) else {}
        prefetch_summary = {
            "target_symbol_count": sum(
                len(screening.symbols) for screening in [primary_screening, *(spec[3] for spec in band_specs)]
                if screening
            ),
            "unique_symbol_count": len(all_symbols),
            "completed_count": len(fetched),
            "elapsed_ms": round(prefetch_elapsed_ms, 3),
            "max_concurrency_configured": config.FILTER_BOARD_MAX_CONCURRENCY,
            "max_concurrency_observed": metrics_after_prefetch.get("max_concurrency_observed"),
        }
        self.logger.info(
            "全価格帯の板一括取得完了: 対象=%d件 ユニーク=%d件 実取得完了=%d件 並列数=%d 所要=%.3fms",
            prefetch_summary["target_symbol_count"], len(all_symbols), len(fetched),
            config.FILTER_BOARD_MAX_CONCURRENCY, prefetch_elapsed_ms,
        )

        primary_usecase.execute(
            target_date=target_date,
            board_retry=board_retry,
            board_metrics_before=metrics_before,
            board_run_metrics_before=metrics_before,
            board_prefetch_summary=prefetch_summary,
        )
        if not board_cache.clear_registrations():
            self.logger.error("通常フィルタ後の登録解除に失敗したため、追加価格帯フィルタを中止します。")
        else:
            for price_cap, cap_name, screening_repository, _, filtering_directory in band_specs:
                band_started = self.perf_counter_clock()
                band_metrics_before = metrics_before_getter() if callable(metrics_before_getter) else None
                try:
                    alternate_usecase = self.filtering_use_case_factory(
                        screening_repository,
                        board_cache,
                        self.volume_client,
                        self.filtering_repository_factory(filtering_directory),
                        diagnostics_repository=self.diagnostics_repository_factory(
                            filtering_directory / "diagnostics"
                        ),
                    )
                    result = alternate_usecase.execute(
                        target_date=target_date,
                        price_cap=price_cap,
                        deadline_monotonic=deadline_monotonic,
                        price_band=cap_name,
                        board_retry=board_retry,
                        board_metrics_before=band_metrics_before,
                        board_run_metrics_before=metrics_before,
                        board_prefetch_summary=prefetch_summary,
                    )
                    self.logger.info(
                        "価格帯別フィルタ完了: 上限=%s円 | 採用=%d件 | 所要=%.3fms | 保存先=%s",
                        cap_name,
                        len(result.symbols),
                        (self.perf_counter_clock() - band_started) * 1000,
                        filtering_directory,
                    )
                except FilteringDeadlineExceeded as exc:
                    self.logger.warning(
                        "価格帯別フィルタを締め切りで打ち切りました: 上限=%s円 | 所要=%.3fms | %s",
                        cap_name,
                        (self.perf_counter_clock() - band_started) * 1000,
                        exc,
                    )
                except Exception:
                    self.logger.exception("価格帯別フィルタに失敗しました: 上限=%s円", cap_name)
        self.logger.info(
            "全価格帯板取得サマリー: 実取得=%d件 | キャッシュ再利用=%d件 | "
            "板取得時間合計=%.3fms | 全処理経過=%.3fms | 登録解除=%d回",
            board_cache.fetch_count - primary_fetch_count,
            board_cache.cache_hit_count - primary_cache_hit_count,
            board_cache.fetch_duration_ms - primary_fetch_duration_ms,
            (self.perf_counter_clock() - started_at) * 1000,
            board_cache.unregister_count,
        )
        if not board_cache.clear_registrations():
            self.logger.error("フィルタ終了時の銘柄登録解除に失敗しました。")