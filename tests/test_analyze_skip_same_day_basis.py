import pytest

from scripts.analysis.analyze_skip_same_day_basis import (
    classify_outcome,
    compare_outcomes,
    minute_same_day_pnl,
    observation_same_day_pnl,
    open_to_close_pnl,
    price_at_or_before,
)
from scripts.analysis.analyze_skip_by_correlation_turnover import (
    assign_exposure_buckets,
    calculate_event_exposures,
    calculate_same_day_result,
    exclude_holiday_events,
    is_exchange_closed_day,
    same_day_exclusion_reason,
    same_day_source_counts,
    summarize_index_bucket_disagreement,
)

DAY = "2026-09-15"


def _bars(*items):
    return [(f"{DAY}T{t}", p) for t, p in items]


def test_open_to_close_pnl():
    assert open_to_close_pnl(200.0, 100, 100.0, 110.0) == pytest.approx(2000.0)
    assert open_to_close_pnl(200.0, 100, 100.0, 90.0) == pytest.approx(-2000.0)
    assert open_to_close_pnl(200.0, 100, None, 90.0) is None
    assert open_to_close_pnl(200.0, 100, 0.0, 90.0) is None


def test_price_at_or_before_has_no_lookahead():
    bars = _bars(("15:10:00", 100.0), ("15:15:00", 101.0), ("15:20:00", 102.0), ("15:22:00", 999.0))
    assert price_at_or_before(bars, f"{DAY}T15:20:00") == 102.0
    assert price_at_or_before(bars, f"{DAY}T15:19:00") == 101.0
    assert price_at_or_before(bars, f"{DAY}T09:00:00") is None
    # 順不同でも最新の過去分を選ぶ
    assert price_at_or_before(list(reversed(bars)), f"{DAY}T15:16:00") == 101.0
    assert price_at_or_before(bars, f"{DAY}T15:20:00", earliest=f"{DAY}T15:21:00") is None


def test_minute_same_day_pnl():
    bars = _bars(("09:35:00", 100.0), ("15:15:00", 95.0), ("15:22:00", 200.0))
    assert minute_same_day_pnl(100.0, 100, bars, f"{DAY}T09:35:04") == (-500.0, "")
    assert minute_same_day_pnl(100.0, 100, bars, f"{DAY}T00:00:00")[0] is None
    assert minute_same_day_pnl(100.0, 100, [], f"{DAY}T09:35:04") == (None, "分足なし")
    stale = _bars(("09:35:00", 100.0))
    assert minute_same_day_pnl(100.0, 100, stale, f"{DAY}T09:35:04")[1] == "15:10〜15:20の分足なし"


def test_observation_same_day_pnl():
    occurred = f"{DAY}T11:47:15"
    assert observation_same_day_pnl(100.0, 10, 103.0, f"{DAY}T15:19:20", occurred) == pytest.approx(30.0)
    assert observation_same_day_pnl(100.0, 10, 103.0, "2026-09-16T15:19:20", occurred) is None
    assert observation_same_day_pnl(100.0, 10, 103.0, f"{DAY}T12:00:00", occurred) is None
    assert observation_same_day_pnl(100.0, 10, None, f"{DAY}T15:19:20", occurred) is None


def test_classify_and_compare_outcomes():
    assert classify_outcome(-1.0) == "損失回避の可能性"
    assert classify_outcome(1.0) == "利益取り逃しの可能性"
    assert classify_outcome(0.0) == "値動きなし"
    assert classify_outcome(None) == "算出不能"
    assert compare_outcomes("損失回避の可能性", "利益取り逃しの可能性") == "損失回避→取り逃し"
    assert compare_outcomes("利益取り逃しの可能性", "損失回避の可能性") == "取り逃し→損失回避"
    assert compare_outcomes("損失回避の可能性", "損失回避の可能性") == "変化なし"
    assert compare_outcomes("損失回避の可能性", "値動きなし") == "その他の変化"
    assert compare_outcomes("未確定", "値動きなし") == "比較不能"
    assert compare_outcomes("損失回避の可能性", "算出不能") == "比較不能"


def test_same_day_result_uses_close_then_intraday_then_daily():
    event = {"reference_price": 100.0, "quantity": 10, "occurred_at": f"{DAY}T09:30:00"}
    bars = _bars(("15:10:00", 104.0), ("15:15:00", 105.0), ("15:22:00", 999.0))

    close_result = calculate_same_day_result(
        {**event, "same_day_close_price": 103.0}, {"open": 80.0, "close": 120.0}, bars
    )
    assert close_result["same_day_pnl"] == pytest.approx(30.0)
    assert close_result["same_day_source"] == "same_day_close_price"

    minute_result = calculate_same_day_result(event, {"open": 80.0, "close": 120.0}, bars)
    assert minute_result["same_day_pnl"] == pytest.approx(50.0)
    assert minute_result["same_day_source"] == "分足B"

    daily_result = calculate_same_day_result(event, {"open": 100.0, "close": 110.0}, [])
    assert daily_result["same_day_pnl"] == pytest.approx(100.0)
    assert daily_result["same_day_source"] == "日足A"


@pytest.mark.parametrize(
    "occurred_at,bars",
    [
        (f"{DAY}T00:00:00", _bars(("15:15:00", 105.0))),
        (f"{DAY}T09:30:00", _bars(("09:35:00", 101.0))),
    ],
)
def test_same_day_result_falls_back_to_daily_when_intraday_is_unusable(occurred_at, bars):
    event = {"reference_price": 100.0, "quantity": 10, "occurred_at": occurred_at}
    result = calculate_same_day_result(event, {"open": 100.0, "close": 120.0}, bars)

    assert result["same_day_pnl"] == pytest.approx(200.0)
    assert result["same_day_source"] == "日足A(フォールバック)"
    assert result["same_day_unavailable_reason"] == ""


def test_same_day_source_counts_preserve_each_basis():
    assert same_day_source_counts([
        {"same_day_source": "分足B"},
        {"same_day_source": "日足A"},
        {"same_day_source": "日足A(フォールバック)"},
        {"same_day_source": "分足B"},
    ]) == {"分足B": 2, "日足A": 1, "日足A(フォールバック)": 1}


def test_exchange_closed_day_filter_includes_weekend_holiday_and_new_year_closure():
    events = [
        {"event_type": "A", "symbol": "1", "occurred_at": "2026-09-21T09:00:00"},
        {"event_type": "B", "symbol": "2", "occurred_at": "2026-09-19T09:00:00"},
        {"event_type": "C", "symbol": "3", "occurred_at": "2026-12-31T09:00:00"},
        {"event_type": "D", "symbol": "4", "occurred_at": "2026-09-24T09:00:00"},
    ]

    retained, excluded = exclude_holiday_events(events)

    assert [event["symbol"] for event in retained] == ["4"]
    assert [event["symbol"] for event in excluded] == ["1", "2", "3"]
    assert is_exchange_closed_day({"occurred_at": "2026-09-21T09:00:00"})


def test_same_day_quality_exclusions():
    assert same_day_exclusion_reason({"same_day_data_quality": "OK"}) == ""
    assert same_day_exclusion_reason({"same_day_data_quality": "BOARD_UNAVAILABLE"}) == "BOARD_UNAVAILABLE"
    assert same_day_exclusion_reason({
        "same_day_data_quality": "DAILY_REPLAY_NO_INTRADAY,BOARD_UNAVAILABLE"
    }) == "DAILY_REPLAY_NO_INTRADAY"


def test_exposure_buckets_are_assigned_independently():
    events = [
        {"corr_^N225": 1.0, "corr_2516.T": 6.0, "corr_1306.T": 1.0, "turnover": 6.0},
        {"corr_^N225": 2.0, "corr_2516.T": 5.0, "corr_1306.T": 2.0, "turnover": 5.0},
        {"corr_^N225": 3.0, "corr_2516.T": 4.0, "corr_1306.T": 3.0, "turnover": 4.0},
        {"corr_^N225": 4.0, "corr_2516.T": 3.0, "corr_1306.T": 4.0, "turnover": 3.0},
        {"corr_^N225": 5.0, "corr_2516.T": 2.0, "corr_1306.T": 5.0, "turnover": 2.0},
        {"corr_^N225": 6.0, "corr_2516.T": 1.0, "corr_1306.T": 6.0, "turnover": 1.0},
    ]
    assign_exposure_buckets(events, ["corr_^N225", "corr_2516.T", "corr_1306.T", "turnover"])

    assert events[0]["corr_^N225_bucket"] == "低"
    assert events[0]["corr_2516.T_bucket"] == "高"
    assert events[0]["corr_1306.T_bucket"] == "低"
    assert events[0]["turnover_bucket"] == "高"


def test_index_bucket_disagreement_summary():
    events = [
        {"corr_^N225_bucket": "低", "corr_2516.T_bucket": "低", "corr_1306.T_bucket": "低"},
        {"corr_^N225_bucket": "低", "corr_2516.T_bucket": "中", "corr_1306.T_bucket": "高"},
        {"corr_^N225_bucket": "低", "corr_2516.T_bucket": "算出不能", "corr_1306.T_bucket": "高"},
    ]

    assert summarize_index_bucket_disagreement(events) == {
        "all_three_available": 2,
        "same_tercile_all_three": 1,
        "different_terciles": 1,
        "at_least_one_unavailable": 1,
    }


def test_exposure_calculation_uses_only_data_before_event_day():
    from datetime import date, timedelta

    days = [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(50)]
    returns = {day: 0.01 * ((index % 5) - 2) for index, day in enumerate(days)}
    closes = {days[0]: 100.0}
    for index, day in enumerate(days[1:], start=1):
        closes[day] = closes[days[index - 1]] * (1.0 + returns[day])
    volumes = {day: 100.0 for day in days}
    cutoff = days[45]
    baseline = calculate_event_exposures(cutoff, returns, closes, volumes, {"^N225": returns})

    changed_returns = {day: (999.0 if day >= cutoff else value) for day, value in returns.items()}
    changed_closes = {day: (1e9 if day >= cutoff else value) for day, value in closes.items()}
    changed_volumes = {day: (1e9 if day >= cutoff else value) for day, value in volumes.items()}
    changed = calculate_event_exposures(
        cutoff, changed_returns, changed_closes, changed_volumes, {"^N225": changed_returns}
    )

    assert changed == baseline
