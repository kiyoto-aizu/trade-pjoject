from datetime import date
import json

import pytest

from src.domain.atr_ratio_analysis import (
    AtrRatioPoint,
    DatedDailyBar,
    calculate_at_or_below_percentile,
    calculate_atr_ratio_series,
    calculate_reference_candidates,
    summarize_atr_ratio_distribution,
    summarize_symbol_atr_ratios,
)
from src.domain.volatility import DailyBar
from src.entrypoints.run_atr_ratio_analysis import build_report, load_filtering_symbols


def test_atr_ratio_series_uses_existing_atr_assessment_at_each_end_date():
    dated_bars = [
        DatedDailyBar(date(2026, 1, 1), DailyBar(101, 99, 100)),
        DatedDailyBar(date(2026, 1, 2), DailyBar(102, 100, 101)),
        DatedDailyBar(date(2026, 1, 3), DailyBar(104, 100, 102)),
        DatedDailyBar(date(2026, 1, 4), DailyBar(108, 100, 104)),
    ]

    points = calculate_atr_ratio_series(dated_bars, period=3)

    assert [(point.date, point.ratio) for point in points] == [
        (date(2026, 1, 3), pytest.approx(1.5)),
        (date(2026, 1, 4), pytest.approx(12 / 7)),
    ]


def test_atr_ratio_distribution_includes_required_summary_statistics():
    summary = summarize_atr_ratio_distribution([1.0, 2.0, 3.0, 4.0])

    assert summary["count"] == 4
    assert summary["mean"] == pytest.approx(2.5)
    assert summary["median"] == pytest.approx(2.5)
    assert summary["standard_deviation"] == pytest.approx(1.2909944487)
    assert summary["percentiles"] == {
        "p10": pytest.approx(1.3),
        "p25": pytest.approx(1.75),
        "p50": pytest.approx(2.5),
        "p75": pytest.approx(3.25),
        "p90": pytest.approx(3.7),
        "p95": pytest.approx(3.85),
    }


def test_threshold_percentile_and_symbol_summaries_are_calculated():
    points_by_symbol = {
        "1111": [AtrRatioPoint(date(2026, 1, 1), 1.0), AtrRatioPoint(date(2026, 1, 2), 3.0)],
        "2222": [AtrRatioPoint(date(2026, 1, 1), 2.0), AtrRatioPoint(date(2026, 1, 2), 4.0)],
    }

    assert calculate_at_or_below_percentile([1.0, 2.0, 3.0, 4.0], 1.5) == pytest.approx(25.0)
    assert calculate_at_or_below_percentile([], 1.5) is None
    assert summarize_symbol_atr_ratios(points_by_symbol) == [
        {"symbol": "1111", "count": 2, "median": 2.0, "p90": pytest.approx(2.8)},
        {"symbol": "2222", "count": 2, "median": 3.0, "p90": pytest.approx(3.8)},
    ]
    assert calculate_reference_candidates([1.0, 2.0, 3.0, 4.0])["normal_upper_ratio"] == pytest.approx(2.5)


def test_report_uses_union_of_filtering_history_and_reports_threshold_positions(tmp_path):
    (tmp_path / "2026-01-01.json").write_text(
        json.dumps({"symbols": ["1111", "2222"]}), encoding="utf-8"
    )
    (tmp_path / "2026-01-02.json").write_text(
        json.dumps({"symbols": ["2222", "3333"]}), encoding="utf-8"
    )
    symbols, history_file_count = load_filtering_symbols(tmp_path)
    dated_bars = [
        DatedDailyBar(date(2026, 1, day), DailyBar(101, 99, 100))
        for day in range(1, 4)
    ]

    class FakeYahooFinanceClient:
        def get_daily_ohlc_history(self, symbol, range_):
            return dated_bars if symbol != "3333" else []

    report = build_report(
        symbols,
        filtering_history_file_count=history_file_count,
        client=FakeYahooFinanceClient(),
        atr_period=3,
        data_range="max",
    )

    assert symbols == ["1111", "2222", "3333"]
    assert report["universe"]["filtering_history_file_count"] == 2
    assert report["universe"]["analyzed_symbol_count"] == 2
    assert report["universe"]["excluded_symbols"] == [
        {"symbol": "3333", "reason": "insufficient_daily_ohlc"}
    ]
    assert report["overall_summary"]["count"] == 2
    assert report["current_threshold_percentiles"]["at_or_below_percentile"]["1.5"] == 100.0
    assert report["current_threshold_percentiles"]["upper_tail_percent"]["2.0"] == 0.0