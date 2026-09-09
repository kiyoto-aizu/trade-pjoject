from datetime import datetime, timedelta

from src.application.filtering_usecase import FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.domain.enums import RankingType
from src.domain.models import FilteringResult, RankingEntry, Regulation, ScreeningResult
from src.domain.rules import calculate_buy_quantity, calculate_volume_surge_ratio, check_kill_switch, exclude_by_regulation, is_buy_order_amount_allowed, limit_candidates, merge_ranking_candidates
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.kabu.ranking_repository import RankingRepository
from src.config import config
from pathlib import Path


class RankingStub:
    def get_ranking(self, ranking_type, exchange_division="ALL"):
        return [
            RankingEntry("7203", 1, 100.0, ranking_type, 100.0),
            RankingEntry("8306", 2, 90.0, ranking_type, 100.0),
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


def test_limit_candidates_caps_the_result_at_fifty():
    candidates = [str(index) for index in range(51)]
    assert limit_candidates(candidates) == candidates[:50]


def test_volume_ratio_and_kill_switch():
    assert calculate_volume_surge_ratio(300, 100) == 3
    assert not check_kill_switch(10, 0, 100_000, config, 1)


def test_order_amount_limit_applies_only_to_buy_orders():
    assert is_buy_order_amount_allowed(10_000, config, api_soft_limit=1_000_000)
    assert not is_buy_order_amount_allowed(10_001, config, api_soft_limit=1_000_000)
    # 売り注文は保有株の決済であり、金額上限では止めない。
    assert check_kill_switch(0, 0, 100_000, config, 100_000, api_soft_limit=1_000_000)


def test_buy_quantity_uses_maximum_affordable_order_units():
    assert calculate_buy_quantity(40, 10_000, 100) == 200
    assert calculate_buy_quantity(101, 10_000, 100) == 0


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

    assert (price_gain.rank, price_gain.value, price_gain.current_price) == (8, 3.25, 2500)
    assert (turnover.rank, turnover.value, turnover.current_price) == (4, 125000.5, 1000)


def test_screening_usecase_persists_date_result(tmp_path):
    repository = ScreeningResultRepository(tmp_path)
    notifications = []
    result = ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(), repository, notifications.append
    ).execute()
    assert result.symbols == ["7203", "8306"]
    assert notifications == [
        "【スクリーニング結果】\n"
        "採用銘柄: 2銘柄\n"
        "候補: 2件\n"
        "除外:\n"
        "  株価上限: 0件（銘柄選定では価格制限なし）\n"
        "  規制: 0件\n"
        "  地方取引所: 0件\n"
        "上位銘柄:\n"
        "- 7203(値上がり率+100%)\n"
        "- 8306(値上がり率+90%)"
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
    notifications = []
    result = FilteringUseCase(
        screening_repository, BoardStub(), VolumeStub(), result_repository, notifications.append
    ).execute()
    assert len(result.symbols) == 10
    assert notifications == [
        "【フィルタリング結果】\n"
        "採用銘柄: 10銘柄\n"
        "スクリーニング対象: 12件\n"
        "出来高条件で除外: 0件\n"
        "上位銘柄:\n"
        "- 0(20日平均売買代金の2.0倍)\n"
        "- 1(20日平均売買代金の2.0倍)\n"
        "- 10(20日平均売買代金の2.0倍)\n"
        "- 11(20日平均売買代金の2.0倍)\n"
        "- 2(20日平均売買代金の2.0倍)"
    ]
    assert result_repository.load_latest().symbols == result.symbols


def test_filtering_usecase_selects_by_relative_turnover_ratio(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while previous_business_day.weekday() >= 5:
        previous_business_day -= timedelta(days=1)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        previous_business_day.isoformat(), ["7689", "6619"], datetime.now().isoformat()
    ))

    class MixedBoardStub:
        def get_current_board(self, symbol):
            volume = 1150 if symbol == "7689" else 90
            return {"current_price": 100, "trading_volume": volume}

    result_repository = FilteringResultRepository(tmp_path / "filtering")
    notifications = []
    result = FilteringUseCase(
        screening_repository, MixedBoardStub(), VolumeStub(), result_repository, notifications.append
    ).execute()

    # 同時刻帯の日足比較は行わず、取得できた候補を相対順位で選ぶ
    assert result.symbols == ["7689", "6619"]
    assert notifications == [
        "【フィルタリング結果】\n"
        "採用銘柄: 2銘柄\n"
        "スクリーニング対象: 2件\n"
        "出来高条件で除外: 0件\n"
        "上位銘柄:\n"
        "- 7689(20日平均売買代金の11.5倍)\n"
        "- 6619(20日平均売買代金の0.9倍)"
    ]


def test_filtering_without_previous_result_saves_empty_result(tmp_path):
    result = FilteringUseCase(
        ScreeningResultRepository(tmp_path / "screening"),
        BoardStub(),
        VolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"),
    ).execute()
    assert result.symbols == []
