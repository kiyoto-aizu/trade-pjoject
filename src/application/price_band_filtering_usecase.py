import logging
from datetime import datetime
from time import monotonic, perf_counter

from src.application.filtering_usecase import FilteringDeadlineExceeded, FilteringUseCase
from src.config import config
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

    def run(self, target_date=None) -> None:
        board_cache = self.board_cache
        primary_fetch_count = board_cache.fetch_count
        primary_cache_hit_count = board_cache.cache_hit_count
        primary_fetch_duration_ms = board_cache.fetch_duration_ms
        additional_started = self.perf_counter_clock()
        if not board_cache.clear_registrations():
            self.logger.error(
                "270円フィルタ保存後の銘柄登録解除に失敗したため、追加価格帯フィルタを中止します。"
            )
        else:
            wall_now = self.now()
            deadline_at = datetime.combine(
                wall_now.date(), config.FILTERING_PRICE_BAND_DEADLINE_TIME
            )
            deadline_monotonic = self.monotonic_clock() + max(
                0.0, (deadline_at - wall_now).total_seconds()
            )
            for price_cap in sorted(config.SCREENING_ALTERNATE_PRICE_CAPS):
                cap_name = f"{price_cap:g}"
                screening_directory = config.SCREENING_PRICE_BAND_RESULT_ROOT / cap_name
                filtering_directory = config.FILTERING_PRICE_BAND_RESULT_ROOT / cap_name
                try:
                    alternate_usecase = self.filtering_use_case_factory(
                        self.screening_repository_factory(screening_directory),
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
                    )
                    self.logger.info(
                        "価格帯別フィルタ完了: 上限=%s円 | 採用=%d件 | 保存先=%s",
                        cap_name,
                        len(result.symbols),
                        filtering_directory,
                    )
                except FilteringDeadlineExceeded as exc:
                    self.logger.warning(
                        "価格帯別フィルタを締め切りで打ち切りました: 上限=%s円 | %s",
                        cap_name,
                        exc,
                    )
                except Exception:
                    self.logger.exception(
                        "価格帯別フィルタに失敗しました: 上限=%s円", cap_name
                    )
        self.logger.info(
            "価格帯別板取得サマリー: 追加ユニーク取得=%d件 | 追加キャッシュ再利用=%d件 | "
            "板取得時間合計=%.3fms | 追加処理経過=%.3fms | 登録解除=%d回",
            board_cache.fetch_count - primary_fetch_count,
            board_cache.cache_hit_count - primary_cache_hit_count,
            board_cache.fetch_duration_ms - primary_fetch_duration_ms,
            (self.perf_counter_clock() - additional_started) * 1000,
            board_cache.unregister_count,
        )
        if not board_cache.clear_registrations():
            self.logger.error("フィルタ終了時の銘柄登録解除に失敗しました。")