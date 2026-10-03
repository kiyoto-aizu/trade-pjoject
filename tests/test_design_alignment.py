from datetime import date, datetime, timedelta
from time import monotonic

import pytest

from src.application.filtering_usecase import FilteringDeadlineExceeded, FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.domain.enums import RankingType
from src.domain.models import FilteringResult, RankingEntry, Regulation, ScreeningResult
from src.infrastructure.market_data.historical_ranking_repository import HistoricalRankingRepository
from src.infrastructure.persistence.listed_security_repository import ListedSecurityRepository
from src.infrastructure.persistence.historical_regulation_repository import HistoricalRegulationRepository
from src.domain.rules import calculate_buy_quantity, calculate_volume_surge_ratio, check_kill_switch, exclude_by_regulation, filter_candidates_by_price, is_buy_order_amount_allowed, limit_candidates, merge_ranking_candidates
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.screening_api_check_repository import ScreeningApiCheckRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository
from src.infrastructure.kabu.ranking_repository import RankingRepository
from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day
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


def test_filter_candidates_by_price_excludes_expensive_and_missing_prices():
    result = filter_candidates_by_price(
        ["7203", "1234", "5678", "9999"],
        {"7203": 270.0, "1234": 271.0, "5678": 0.0},
        270.0,
    )

    assert result.remaining == ["7203"]
    assert result.excluded_by_price_count == 1
    assert result.excluded_missing_price_count == 2


def test_screening_price_cap_uses_current_budget_settings(monkeypatch):
    monkeypatch.setattr(config, "OPERATING_CAPITAL", 100_000.0)
    monkeypatch.setattr(config, "TARGET_POSITIONS", 3)
    monkeypatch.setattr(config, "MAX_ORDER_AMOUNT_PER_TRADE", 30_000.0)
    monkeypatch.setattr(config, "ORDER_UNIT", 100)
    monkeypatch.setattr(config, "SCREENING_PRICE_MARGIN", 0.9)

    assert config.get_screening_price_cap() == 270.0


def test_limit_candidates_caps_the_result_at_fifty():
    candidates = [str(index) for index in range(51)]
    assert limit_candidates(candidates) == candidates[:50]


def test_volume_ratio_and_kill_switch():
    assert calculate_volume_surge_ratio(300, 100) == 3
    assert not check_kill_switch(10, 0, 100_000, config)


def test_order_amount_limit_applies_only_to_buy_orders():
    assert is_buy_order_amount_allowed(30_000, config, api_soft_limit=1_000_000)
    assert not is_buy_order_amount_allowed(30_001, config, api_soft_limit=1_000_000)
    # 売り注文は保有株の決済であり、金額上限では止めない。
    assert check_kill_switch(0, 0, 100_000, config)


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


def test_listed_security_repository_builds_a_date_specific_universe(tmp_path):
    master_path = tmp_path / "listed_securities.csv"
    master_path.write_text(
        "symbol,exchange_division,listed_from,listed_to\n"
        "7203,TP,2020-01-01,\n"
        "8306,TS,2020-01-01,2026-09-07\n"
        "6758,TP,2026-09-08,\n",
        encoding="utf-8",
    )

    securities = ListedSecurityRepository(master_path).load_for_date(date(2026, 9, 8))

    assert [(security.symbol, security.exchange_division) for security in securities] == [
        ("7203", "TP"),
        ("6758", "TP"),
    ]


def test_historical_ranking_uses_master_universe_and_daily_values(tmp_path):
    master_path = tmp_path / "listed_securities.csv"
    master_path.write_text(
        "symbol,exchange_division,listed_from,listed_to\n"
        "7203,TP,2020-01-01,\n"
        "8306,TS,2020-01-01,\n",
        encoding="utf-8",
    )

    class MarketDataStub:
        def get_daily_market_data(self, symbol, target_date):
            values = {
                "7203": {"close": 110, "previous_close": 100, "volume": 2},
                "8306": {"close": 90, "previous_close": 100, "volume": 5},
            }
            return values[symbol]

    repository = HistoricalRankingRepository(
        ListedSecurityRepository(master_path), MarketDataStub()
    )

    turnover = repository.get_ranking(RankingType.TURNOVER, "ALL", date(2026, 9, 8))
    price_gain = repository.get_ranking(RankingType.PRICE_GAIN, "ALL", date(2026, 9, 8))

    assert [entry.symbol for entry in turnover] == ["8306", "7203"]
    assert [entry.symbol for entry in price_gain] == ["7203", "8306"]


def test_historical_regulation_repository_reads_date_ranges(tmp_path):
    master_path = tmp_path / "historical_regulations.csv"
    master_path.write_text(
        "symbol,primary_exchange,restricted_from,restricted_to,reason\n"
        "7203,1,2026-09-01,2026-09-07,売買規制\n"
        "7203,1,2026-09-10,,監視措置\n",
        encoding="utf-8",
    )
    repository = HistoricalRegulationRepository(master_path)

    restricted = repository.get_regulation("7203", 1, date(2026, 9, 5))
    unrestricted = repository.get_regulation("7203", 1, date(2026, 9, 8))

    assert (restricted.is_restricted, restricted.reason) == (True, "売買規制")
    assert unrestricted.is_restricted is False


def test_screening_usecase_persists_date_result(tmp_path):
    repository = ScreeningResultRepository(tmp_path)
    notifications = []
    result = ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(), repository, notifications.append
    ).execute()
    assert result.symbols == ["7203", "8306"]
    assert notifications
    assert notifications[0].startswith("【業務】銘柄選定\n【機能】スクリーニング\n")
    assert "採用銘柄数: 2件" in notifications[0]
    assert "候補数: 2件" in notifications[0]
    assert "- 7203(値上がり率 +100.00%, 売買代金 0.00億円)" in notifications[0]
    saved_result = repository.load_latest()
    assert saved_result.symbols == result.symbols
    assert [(entry.symbol, entry.total_rank, entry.selected) for entry in saved_result.audit_entries] == [
        ("7203", 2, True),
        ("8306", 4, True),
    ]
    assert [entry.price for entry in saved_result.audit_entries] == [100.0, 100.0]


def test_screening_result_loads_audit_entries_without_price():
    result = ScreeningResult.from_dict({
        "date": "2026-09-01",
        "symbols": ["7203"],
        "generated_at": "2026-09-01T15:35:00",
        "audit_entries": [{
            "symbol": "7203",
            "turnover_rank": 1,
            "turnover_value": 100.0,
            "price_gain_rank": 1,
            "price_gain_value": 1.0,
            "total_rank": 2,
            "primary_exchange": 1,
            "is_restricted": False,
            "restriction_reason": "",
            "selected": True,
        }],
    })

    assert result.audit_entries[0].price is None


def test_screening_usecase_filters_prices_before_regulation_lookups(monkeypatch, tmp_path):
    class PriceRankingStub:
        def get_ranking(self, ranking_type, exchange_division="ALL"):
            return [
                RankingEntry("cheap", 1, 100.0, ranking_type, 200.0),
                RankingEntry("expensive", 2, 90.0, ranking_type, 400.0),
                RankingEntry("missing", 3, 80.0, ranking_type, None),
            ]

    checked_symbols = []

    class TrackingRegulationStub:
        def get_regulation(self, symbol, market_code):
            checked_symbols.append(symbol)
            return Regulation(symbol, False)

    monkeypatch.setattr(config, "OPERATING_CAPITAL", 100_000.0)
    monkeypatch.setattr(config, "TARGET_POSITIONS", 3)
    monkeypatch.setattr(config, "MAX_ORDER_AMOUNT_PER_TRADE", 30_000.0)
    monkeypatch.setattr(config, "ORDER_UNIT", 100)
    monkeypatch.setattr(config, "SCREENING_PRICE_MARGIN", 1.0)
    result = ScreeningUseCase(
        PriceRankingStub(), TrackingRegulationStub(), ExchangeStub(),
        ScreeningResultRepository(tmp_path),
    ).execute()

    assert result.symbols == ["cheap"]
    assert checked_symbols == ["cheap"]
    reasons = {entry.symbol: entry.restriction_reason for entry in result.audit_entries}
    assert reasons["expensive"] == "価格上限超過（300.0円）"
    assert reasons["missing"] == "価格不明"


def test_screening_can_keep_api_unconfirmed_candidate_for_price_band(tmp_path):
    class UnconfirmedExchangeStub:
        def get_primary_exchange(self, symbol):
            return None

        def status_for(self, symbol):
            return "unconfirmed_market"

    result = ScreeningUseCase(
        RankingStub(),
        RegulationStub(),
        UnconfirmedExchangeStub(),
        ScreeningResultRepository(tmp_path),
    ).execute(price_cap=450.0, keep_unconfirmed=True)

    assert result.symbols == ["7203", "8306"]
    assert all(entry.check_status == "unconfirmed_market" for entry in result.audit_entries)
    assert all(entry.is_restricted is False for entry in result.audit_entries)


def test_screening_price_band_caps_select_correct_prices_and_record_each_price(tmp_path):
    class ThreePriceRankingStub:
        def get_ranking(self, ranking_type, exchange_division="ALL"):
            return [
                RankingEntry("under450", 1, 300.0, ranking_type, 400.0),
                RankingEntry("under900", 2, 200.0, ranking_type, 800.0),
                RankingEntry("over900", 3, 100.0, ranking_type, 1_000.0),
            ]

    def run_at_cap(cap, directory):
        return ScreeningUseCase(
            ThreePriceRankingStub(),
            RegulationStub(),
            ExchangeStub(),
            ScreeningResultRepository(directory),
        ).execute(price_cap=cap)

    result_450 = run_at_cap(450.0, tmp_path / "450")
    result_900 = run_at_cap(900.0, tmp_path / "900")

    assert result_450.symbols == ["under450"]
    assert result_900.symbols == ["under450", "under900"]
    assert {entry.symbol: entry.price for entry in result_450.audit_entries} == {
        "under450": 400.0,
        "under900": 800.0,
        "over900": 1_000.0,
    }


def test_screening_market_checks_are_registered_in_configured_batches(monkeypatch, tmp_path):
    class ManyCandidateRankingStub:
        def get_ranking(self, ranking_type, exchange_division="ALL"):
            return [
                RankingEntry(str(index), index, 100.0, ranking_type, 100.0)
                for index in range(1, 6)
            ]

    monkeypatch.setattr(config, "SCREENING_BATCH_SIZE", 2)
    monkeypatch.setattr("src.application.screening_usecase.sleep", lambda _seconds: None)
    started_batches = []
    finished_batches = []
    usecase = ScreeningUseCase(
        ManyCandidateRankingStub(),
        RegulationStub(),
        ExchangeStub(),
        ScreeningResultRepository(tmp_path),
    )
    usecase.batch_started = lambda batch, number: started_batches.append((number, batch)) or True
    usecase.batch_finished = lambda batch, number: finished_batches.append((number, batch)) or True

    usecase.execute(price_cap=900.0)

    assert [len(batch) for _, batch in started_batches] == [2, 2, 1]
    assert finished_batches == started_batches


def test_screening_api_checks_are_saved_and_reused_for_same_day(tmp_path):
    target_date = date(2026, 10, 2)
    calls = {"exchange": 0, "regulation": 0}

    class ExchangeRepositoryStub:
        def get_primary_exchange(self, symbol):
            calls["exchange"] += 1
            return 1

    class RegulationRepositoryStub:
        def get_regulation(self, symbol, market_code):
            calls["regulation"] += 1
            return Regulation(symbol, False, "", market_code)

    cache = ScreeningApiCheckRepository(
        tmp_path, target_date, ExchangeRepositoryStub(), RegulationRepositoryStub()
    )
    assert cache.get_primary_exchange("7203") == 1
    assert cache.get_regulation("7203", 1, target_date).is_restricted is False
    assert cache.status_for("7203") == "checked"

    class MustNotCallRepository:
        def get_primary_exchange(self, _symbol):
            raise AssertionError("same-day market lookup repeated")

        def get_regulation(self, _symbol, _market_code):
            raise AssertionError("same-day regulation lookup repeated")

    reused_cache = ScreeningApiCheckRepository(
        tmp_path, target_date, MustNotCallRepository(), MustNotCallRepository()
    )
    assert reused_cache.get_primary_exchange("7203") == 1
    assert reused_cache.get_regulation("7203", 1, target_date).is_restricted is False
    assert calls == {"exchange": 1, "regulation": 1}
    import json
    saved = json.loads((tmp_path / "2026-10-02.json").read_text(encoding="utf-8"))
    assert saved["checks"]["7203"]["primary_exchange"]["status"] == "checked"
    assert saved["checks"]["7203"]["regulation"]["status"] == "checked"


def test_screening_regulation_api_failure_is_retained_as_unconfirmed(tmp_path):
    target_date = date(2026, 10, 2)
    calls = {"regulation": 0}

    class ExchangeRepositoryStub:
        def get_primary_exchange(self, _symbol):
            return 1

    class FailedRegulationRepositoryStub:
        def get_regulation(self, symbol, market_code):
            calls["regulation"] += 1
            return Regulation(symbol, True, "規制情報取得失敗", market_code)

    cache = ScreeningApiCheckRepository(
        tmp_path, target_date, ExchangeRepositoryStub(), FailedRegulationRepositoryStub()
    )
    result = ScreeningUseCase(
        RankingStub(), cache, cache, ScreeningResultRepository(tmp_path / "screening")
    ).execute(price_cap=450.0, keep_unconfirmed=True)

    assert result.symbols == ["7203", "8306"]
    assert all(entry.check_status == "unconfirmed_regulation" for entry in result.audit_entries)
    assert all(entry.is_restricted is False for entry in result.audit_entries)
    assert calls["regulation"] == 2

    class MustNotCallRegulationRepository:
        def get_primary_exchange(self, _symbol):
            raise AssertionError("same-day market lookup repeated")

        def get_regulation(self, _symbol, _market_code):
            raise AssertionError("same-day failed regulation lookup repeated")

    reused_cache = ScreeningApiCheckRepository(
        tmp_path, target_date, MustNotCallRegulationRepository(), MustNotCallRegulationRepository()
    )
    assert reused_cache.get_primary_exchange("7203") == 1
    assert reused_cache.get_regulation("7203", 1).reason == "規制情報取得失敗"
    assert reused_cache.status_for("7203") == "unconfirmed_regulation"


def test_primary_screening_keeps_legacy_fail_closed_regulation_behavior(tmp_path):
    class FailedRegulationStub:
        def get_regulation(self, symbol, market_code):
            return Regulation(symbol, True, "規制情報取得失敗", market_code)

    result = ScreeningUseCase(
        RankingStub(),
        FailedRegulationStub(),
        ExchangeStub(),
        ScreeningResultRepository(tmp_path),
    ).execute(price_cap=450.0)

    assert result.symbols == []
    assert all(entry.is_restricted for entry in result.audit_entries)
    assert all(entry.restriction_reason == "規制情報取得失敗" for entry in result.audit_entries)


def test_filtering_usecase_reads_previous_screening_result(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while not is_trading_day(previous_business_day):
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
    assert notifications
    assert notifications[0].startswith("【業務】銘柄選定\n【機能】フィルタリング\n")
    assert "採用銘柄数: 10件" in notifications[0]
    assert "入力銘柄数: 12件" in notifications[0]
    assert "評価完了数: 12件" in notifications[0]
    assert "評価対象外数: 0件" in notifications[0]
    assert result_repository.load_latest().symbols == result.symbols


def test_filtering_usecase_replays_a_past_date_from_daily_turnover(tmp_path):
    target_date = datetime(2026, 9, 8).date()
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        "2026-09-07", ["7203"], datetime.now().isoformat()
    ))

    class HistoricalVolumeStub:
        def get_turnover_for_date(self, symbol, requested_date):
            assert requested_date == target_date
            return 300.0

        def get_average_turnover_before(self, symbol, requested_date, days):
            assert requested_date == target_date
            assert days == 20
            return 100.0

    result = FilteringUseCase(
        screening_repository,
        BoardStub(),
        HistoricalVolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"),
    ).execute(target_date=target_date)

    assert result.date == "2026-09-08"
    assert result.symbols == ["7203"]


def test_filtering_usecase_skips_holidays_when_locating_previous_screening(tmp_path):
    # 2026-09-21(月・敬老の日)〜09-23(水・秋分の日)は祝日、直前の営業日は09-18(金)
    target_date = datetime(2026, 9, 24).date()
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        "2026-09-18", ["7203"], datetime.now().isoformat()
    ))

    class HistoricalVolumeStub:
        def get_turnover_for_date(self, symbol, requested_date):
            assert requested_date == target_date
            return 300.0

        def get_average_turnover_before(self, symbol, requested_date, days):
            assert requested_date == target_date
            assert days == 20
            return 100.0

    result = FilteringUseCase(
        screening_repository,
        BoardStub(),
        HistoricalVolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"),
    ).execute(target_date=target_date)

    # 土日しか見ない旧ロジックだと09-23(祝日)を見に行き空振りしていた
    assert result.symbols == ["7203"]


def test_filtering_usecase_selects_by_relative_turnover_ratio(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while not is_trading_day(previous_business_day):
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
    assert notifications
    assert notifications[0].startswith("【業務】銘柄選定\n【機能】フィルタリング\n")
    assert "採用銘柄数: 2件" in notifications[0]
    assert "入力銘柄数: 2件" in notifications[0]
    assert "評価完了数: 2件" in notifications[0]
    assert "評価対象外数: 0件" in notifications[0]


def test_filtering_diagnostics_record_sources_reasons_and_rank_without_changing_filter(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while not is_trading_day(previous_business_day):
        previous_business_day -= timedelta(days=1)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        previous_business_day.isoformat(),
        ["value", "fallback", "zero", "board_error", "board_none", "value_missing", "price_missing"],
        datetime.now().isoformat(),
    ))

    class DiagnosticBoardStub:
        def get_current_board(self, symbol):
            if symbol == "board_error":
                raise TimeoutError
            if symbol == "board_none":
                return None
            if symbol == "value":
                return {"current_price": 200, "trading_value": 2_000, "trading_volume": 10}
            if symbol == "fallback":
                return {"current_price": 50, "trading_value": None, "trading_volume": 10}
            if symbol == "zero":
                return {"current_price": 100, "trading_value": 0, "trading_volume": 0}
            if symbol == "price_missing":
                return {"current_price": None, "trading_value": None, "trading_volume": 10}
            return {"current_price": 100, "trading_value": None, "trading_volume": None}

    result_repository = FilteringResultRepository(tmp_path / "filtering")
    diagnostics_repository = FilteringDiagnosticsRepository(tmp_path / "diagnostics")
    result = FilteringUseCase(
        screening_repository,
        DiagnosticBoardStub(),
        VolumeStub(),
        result_repository,
        diagnostics_repository=diagnostics_repository,
    ).execute()

    assert result.symbols == ["fallback", "value", "zero"]
    assert result_repository.load_latest().symbols == result.symbols
    files = list((tmp_path / "diagnostics").glob("*.json"))
    assert len(files) == 1
    import json
    diagnostics = json.loads(files[0].read_text(encoding="utf-8"))
    records = {item["symbol"]: item for item in diagnostics["candidates"]}
    assert records["value"]["numerator_source"] == "TradingValue"
    assert records["fallback"]["numerator_source"] == "current_price_x_cumulative_volume"
    assert records["fallback"]["board_current_price"] == 50
    assert records["value"]["average_includes_target_date"] is None
    assert records["value"]["rank"] == 2
    assert records["value"]["selected"] is True
    assert records["zero"]["reason_code"] == "FILTER_NO_TRADES"
    assert records["board_error"]["reason_code"] == "FILTER_BOARD_FETCH_FAILED"
    assert records["board_error"]["error_type"] == "TimeoutError"
    assert records["board_none"]["reason_code"] == "FILTER_BOARD_FETCH_FAILED"
    assert records["value_missing"]["reason_code"] == "FILTER_TURNOVER_MISSING"
    assert records["price_missing"]["reason_code"] == "FILTER_CURRENT_PRICE_MISSING"
    assert diagnostics["summary"]["reason_counts"] == {
        "FILTER_BOARD_FETCH_FAILED": 2,
        "FILTER_TURNOVER_MISSING": 1,
        "FILTER_CURRENT_PRICE_MISSING": 1,
        "FILTER_NO_TRADES": 1,
    }


def test_filtering_diagnostics_save_failure_does_not_stop_filter_result(tmp_path):
    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while not is_trading_day(previous_business_day):
        previous_business_day -= timedelta(days=1)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        previous_business_day.isoformat(), ["7203"], datetime.now().isoformat()
    ))

    class FailingDiagnosticsRepository:
        def save(self, _diagnostics):
            raise OSError("diagnostics unavailable")

    result_repository = FilteringResultRepository(tmp_path / "filtering")
    result = FilteringUseCase(
        screening_repository,
        BoardStub(),
        VolumeStub(),
        result_repository,
        diagnostics_repository=FailingDiagnosticsRepository(),
    ).execute()

    assert result.symbols == ["7203"]
    assert result_repository.load_latest().symbols == ["7203"]


def test_filtering_deadline_saves_partial_diagnostics_without_filter_result(tmp_path):
    target_date = date(2026, 10, 2)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        "2026-10-01", ["7203", "8306"], datetime.now().isoformat()
    ))

    class ResultRepositoryStub:
        saved = []

        def save(self, result):
            self.saved.append(result)

    class DiagnosticsRepositoryStub:
        saved = []

        def save(self, diagnostics):
            self.saved.append(diagnostics)

    class MustNotFetchBoard:
        def get_current_board(self, _symbol):
            raise AssertionError("deadline should be checked before the next board call")

    result_repository = ResultRepositoryStub()
    diagnostics_repository = DiagnosticsRepositoryStub()
    usecase = FilteringUseCase(
        screening_repository,
        MustNotFetchBoard(),
        VolumeStub(),
        result_repository,
        diagnostics_repository=diagnostics_repository,
    )

    with pytest.raises(FilteringDeadlineExceeded):
        usecase.execute(
            target_date=target_date,
            price_cap=450.0,
            deadline_monotonic=monotonic() - 1,
            price_band="450",
        )

    assert result_repository.saved == []
    assert len(diagnostics_repository.saved) == 1
    diagnostics = diagnostics_repository.saved[0]
    assert diagnostics["price_band"] == "450"
    assert diagnostics["result_saved"] is False
    assert diagnostics["summary"]["timed_out"] is True
    assert diagnostics["summary"]["stop_reason"] == "FILTER_TIME_LIMIT"
    assert [candidate["status"] for candidate in diagnostics["candidates"]] == [
        "not_evaluated",
        "not_evaluated",
    ]
    assert all(candidate["reason_code"] == "FILTER_TIME_LIMIT" for candidate in diagnostics["candidates"])


def test_filtering_without_previous_result_saves_empty_result(tmp_path):
    result = FilteringUseCase(
        ScreeningResultRepository(tmp_path / "screening"),
        BoardStub(),
        VolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"),
    ).execute()
    assert result.symbols == []
