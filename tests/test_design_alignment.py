from datetime import datetime, timedelta

from src.application.filtering_usecase import FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.domain.enums import RankingType
from src.domain.models import FilteringResult, RankingEntry, Regulation, ScreeningResult
from src.domain.rules import calculate_volume_surge_ratio, check_kill_switch, exclude_by_price_ceiling, exclude_by_regulation, limit_candidates, merge_ranking_candidates
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.kabu.ranking_repository import RankingRepository
from src.config import config
from pathlib import Path


class RankingStub:
    def get_ranking(self, ranking_type):
        return [
            RankingEntry("7203", 1, 100.0, ranking_type),
            RankingEntry("8306", 2, 90.0, ranking_type),
        ]


class RegulationStub:
    def get_regulation(self, symbol, market_code):
        assert market_code == 1
        return Regulation(symbol, False)


class ExchangeStub:
    def get_primary_exchange(self, symbol):
        return 1


class BoardStub:
    def get_current_price(self, symbol):
        return 100

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


def test_exclude_by_regulation_counts_each_reason():
    result = exclude_by_regulation(
        ["7203", "1234", "5678"],
        {
            "7203": Regulation("7203", False, primary_exchange=1),
            "1234": Regulation("1234", True),
            "5678": Regulation("5678", False, primary_exchange=3),
        },
    )
    assert result.remaining == ["7203"]
    assert result.excluded_by_regulation_count == 1
    assert result.excluded_by_exchange_count == 1


def test_exclude_by_price_ceiling_excludes_expensive_and_unavailable_prices():
    result = exclude_by_price_ceiling(
        ["7203", "1234", "5678"], {"7203": 300, "1234": 301}, 300
    )
    assert result.remaining == ["7203"]
    assert result.excluded_by_price_count == 2


def test_limit_candidates_caps_the_result_at_fifty():
    candidates = [str(index) for index in range(51)]
    assert limit_candidates(candidates) == candidates[:50]


def test_volume_ratio_and_kill_switch():
    assert calculate_volume_surge_ratio(300, 100) == 3
    assert not check_kill_switch(10, 0, 1_000_000, config, 1)


def test_config_loads_env_from_repository_root():
    repository_root = Path(__file__).resolve().parents[1]
    assert config._env_path == repository_root / ".env"


def test_ranking_repository_uses_api_rank_and_type_specific_value(monkeypatch):
    responses = {
        "1": {"Ranking": [{"Symbol": "7203", "No": 8, "ChangePercentage": 3.25, "CurrentPrice": 2500}]},
        "4": {"Ranking": [{"Symbol": "8306", "No": 4, "Turnover": 125000.5, "CurrentPrice": 1000}]},
    }

    def send_get_stub(url, params, headers):
        assert url == f"{config.BASE_URL}/ranking"
        assert headers == {"X-API-KEY": "test-token"}
        return responses[params["Type"]]

    monkeypatch.setattr("src.infrastructure.kabu.ranking_repository.request_handler.send_get", send_get_stub)
    repository = RankingRepository("test-token")

    price_gain = repository.get_ranking(RankingType.PRICE_GAIN)[0]
    turnover = repository.get_ranking(RankingType.TURNOVER)[0]

    assert (price_gain.rank, price_gain.value) == (8, 3.25)
    assert (turnover.rank, turnover.value) == (4, 125000.5)


def test_screening_usecase_persists_date_result(tmp_path):
    repository = ScreeningResultRepository(tmp_path)
    notifications = []
    result = ScreeningUseCase(
        RankingStub(), BoardStub(), RegulationStub(), ExchangeStub(), repository, notifications.append
    ).execute()
    assert result.symbols == ["7203", "8306"]
    assert notifications == [
        "スクリーニング完了: 2銘柄（候補2件中、高額(300円超)0件・規制0件・地方取引所0件を除外）\n"
        "上位: 7203(値上がり率+100%) / 8306(値上がり率+90%)"
    ]
    saved_result = repository.load_latest()
    assert saved_result.symbols == result.symbols
    assert [(entry.symbol, entry.total_rank, entry.selected) for entry in saved_result.audit_entries] == [
        ("7203", 2, True),
        ("8306", 4, True),
    ]


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
