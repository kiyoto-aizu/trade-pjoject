from datetime import date

from src.domain.models import FilteringResult
from src.entrypoints import backtest_v2_multi_day_check as multi_day_check
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository


def test_discover_days_skips_missing_results_and_non_trading_holiday(tmp_path, monkeypatch):
    filtering_directory = tmp_path / "filtering"
    repository = FilteringResultRepository(filtering_directory)
    repository.save(FilteringResult(date="2026-09-01", symbols=["1111"], generated_at=""))
    repository.save(FilteringResult(date="2026-09-21", symbols=["2222"], generated_at=""))
    repository.save(FilteringResult(date="2026-09-25", symbols=["3333"], generated_at=""))
    monkeypatch.setattr(multi_day_check, "FILTERING_DIRECTORY", filtering_directory)

    target_days, skipped_days = multi_day_check._discover_days(
        date(2026, 9, 1),
        date(2026, 9, 25),
    )

    assert list(target_days) == [date(2026, 9, 1), date(2026, 9, 25)]
    assert {item["date"] for item in skipped_days if item["reason"] == "filtering_result_missing"} >= {
        "2026-09-03"
    }
    assert {item["date"] for item in skipped_days if item["reason"] == "non_trading_day"} >= {
        "2026-09-21"
    }


def test_calculate_realized_pnl_from_paper_orders_includes_weighted_cost_and_fees():
    orders = [
        {"Symbol": "1111", "Side": "2", "Qty": 100, "Price": 100.0, "Fee": 10.0},
        {"Symbol": "1111", "Side": "2", "Qty": 100, "Price": 110.0, "Fee": 11.0},
        {"Symbol": "1111", "Side": "1", "Qty": 100, "Price": 120.0, "Fee": 12.0},
        {"Symbol": "1111", "Side": "1", "Qty": 100, "Price": 120.0, "Fee": 12.0},
    ]

    assert multi_day_check._calculate_realized_pnl_from_orders(orders) == 2_955.0


def test_calculate_realized_pnl_rejects_unclosed_position():
    orders = [
        {"Symbol": "1111", "Side": "2", "Qty": 100, "Price": 100.0, "Fee": 10.0},
    ]

    try:
        multi_day_check._calculate_realized_pnl_from_orders(orders)
    except AssertionError as exc:
        assert "保有が残っています" in str(exc)
    else:
        raise AssertionError("未決済ポジションは検証失敗として扱う必要があります")