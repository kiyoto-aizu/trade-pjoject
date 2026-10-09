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
        # 朝の追加帯は既定で停止(昼のYahoo分足推定へ移行)。停止中は追加帯の板取得・診断・結果保存を行わない
        alternate_caps = (
            sorted(config.SCREENING_ALTERNATE_PRICE_CAPS)
            if config.FILTERING_MORNING_ALTERNATE_BANDS_ENABLED else []
        )
        for price_cap in alternate_caps:
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

        metrics_before_getter = getattr(board_cache, "get_metrics_snapshot", None)
        metrics_before = metrics_before_getter() if callable(metrics_before_getter) else None
        prefetched: set[str] = set()

        def prefetch_band(band_label, screening, should_start=None):
            """その帯の板だけを取得する。前の帯で取得済みの銘柄はキャッシュを使い再取得しない。"""
            symbols = list(dict.fromkeys(str(symbol) for symbol in screening.symbols)) if screening else []
            pending = [symbol for symbol in symbols if symbol not in prefetched]
            board_cache.current_band = band_label
            started = self.perf_counter_clock()
            kwargs = {"should_start": should_start} if should_start is not None else {}
            fetched = board_cache.fetch_current_boards(
                pending, max_workers=config.FILTER_BOARD_MAX_CONCURRENCY, **kwargs
            ) if pending else {}
            elapsed_ms = (self.perf_counter_clock() - started) * 1000
            prefetched.update(fetched)
            metrics_after = metrics_before_getter() if callable(metrics_before_getter) else {}
            summary = {
                "band": band_label,
                "target_symbol_count": len(screening.symbols) if screening else 0,
                "unique_symbol_count": len(symbols),
                "reused_from_earlier_band_count": len(symbols) - len(pending),
                "completed_count": len(fetched),
                "not_started_count": len(pending) - len(fetched),
                "elapsed_ms": round(elapsed_ms, 3),
                "max_concurrency_configured": config.FILTER_BOARD_MAX_CONCURRENCY,
                "max_concurrency_observed": metrics_after.get("max_concurrency_observed"),
            }
            self.logger.info(
                "板取得完了: 帯=%s円 対象=%d件 前の帯から再利用=%d件 実取得完了=%d件 未着手=%d件 並列数=%d 所要=%.3fms",
                band_label, summary["unique_symbol_count"], summary["reused_from_earlier_band_count"],
                len(fetched), summary["not_started_count"], config.FILTER_BOARD_MAX_CONCURRENCY, elapsed_ms,
            )
            return summary

        try:
            primary_label = f"{config.get_screening_price_cap():g}"
        except Exception:
            primary_label = "primary"
        # 270円帯などの主帯を先に取得・評価・リトライまで終え、結果を確定させてから次の帯へ進む
        primary_summary = prefetch_band(primary_label, primary_screening)
        primary_usecase.execute(
            target_date=target_date,
            board_retry=board_retry,
            board_metrics_before=metrics_before,
            board_run_metrics_before=metrics_before,
            board_prefetch_summary=primary_summary,
        )
        primary_cleared = board_cache.clear_registrations()
        if not primary_cleared:
            self.logger.error("通常フィルタ後の登録解除に失敗したため、追加価格帯フィルタを中止します。")
        else:
            for price_cap, cap_name, screening_repository, band_screening, filtering_directory in band_specs:
                band_started = self.perf_counter_clock()
                band_metrics_before = metrics_before_getter() if callable(metrics_before_getter) else None
                try:
                    # 締め切り後の帯は板取得を始めず、executeに未処理銘柄を診断へ残させる
                    band_summary = prefetch_band(
                        cap_name, band_screening,
                        should_start=lambda: self.monotonic_clock() < deadline_monotonic,
                    )
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
                        board_prefetch_summary=band_summary,
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
        # 追加帯を実行しておらず主帯直後の解除が成功していれば、登録は既に空
        if (band_specs or not primary_cleared) and not board_cache.clear_registrations():
            self.logger.error("フィルタ終了時の銘柄登録解除に失敗しました。")