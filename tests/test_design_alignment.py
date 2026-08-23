from datetime import datetime, timedelta

from src.application.filtering_usecase import FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.domain.enums import RankingType
from src.domain.models import FilteringResult, RankingEntry, Regulation, ScreeningResult
from src.domain.rules import calculate_volume_surge_ratio, check_kill_switch, merge_ranking_candidates
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.config import config


class RankingStub:
    def get_ranking(self, ranking_type):
        return [
            RankingEntry("7203", 1, 100.0, ranking_type),
            RankingEntry("8306", 2, 90.0, ranking_type),
        ]


class RegulationStub:
    def get_regulation(self, symbol):
        return Regulation(symbol, False)


class ExchangeStub:
    def get_primary_exchange(self, symbol):
        return 1


class BoardStub:
    def get_current_board(self, symbol):
        return {"current_price": 100, "trading_volume": 200}


class VolumeStub:
    def get_average_volume(self, symbol, days):
        assert days == 20
        return 100


def test_merge_ranking_candidates_uses_rank_sum():
    turnover = [RankingEntry("7203", 1, 0, RankingType.TURNOVER)]
    gain = [RankingEntry("8306", 1, 0, RankingType.PRICE_GAIN)]
    assert merge_ranking_candidates(turnover, gain) == ["7203", "8306"]


def test_volume_ratio_and_kill_switch():
    assert calculate_volume_surge_ratio(300, 100) == 3
    assert not check_kill_switch(10, 0, 1_000_000, config, 1)


def test_screening_usecase_persists_date_result(tmp_path):
    repository = ScreeningResultRepository(tmp_path)
    result = ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(), repository
    ).execute()
    assert result.symbols == ["7203", "8306"]
    assert repository.load_latest().symbols == result.symbols


def test_filtering_usecase_reads_previous_screening_result(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while previous_business_day.weekday() >= 5:
        previous_business_day -= timedelta(days=1)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        previous_business_day.isoformat(), [str(index) for index in range(12)], datetime.now().isoformat()
    ))
    result_repository = FilteringResultRepository(tmp_path / "filtering")
    result = FilteringUseCase(
        screening_repository, BoardStub(), VolumeStub(), result_repository
    ).execute()
    assert len(result.symbols) == 10
    assert result_repository.load_latest().symbols == result.symbols


def test_filtering_without_previous_result_saves_empty_result(tmp_path):
    result = FilteringUseCase(
        ScreeningResultRepository(tmp_path / "screening"),
        BoardStub(),
        VolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"),
    ).execute()
    assert result.symbols == []
