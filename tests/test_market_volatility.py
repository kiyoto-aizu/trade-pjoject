from datetime import date

import pytest

from src.domain.market_volatility import (
    DatedClose,
    calculate_daily_returns,
    calculate_pearson_correlation,
    calculate_previous_day_changes,
    calculate_realized_volatility,
    calculate_realized_volatility_series,
    summarize_distribution,
)


def test_daily_returns_are_percentage_changes():
    assert calculate_daily_returns([100.0, 110.0, 99.0]) == pytest.approx([10.0, -10.0])


def test_realized_volatility_uses_sample_stddev_and_annualizes():
    closes = [100.0, 101.0, 99.0, 100.0]
    expected = pytest.approx(27.36037213465439)

    assert calculate_realized_volatility(closes, window=3) == expected


def test_realized_volatility_returns_none_when_one_extra_close_is_missing():
    assert calculate_realized_volatility([100.0, 101.0, 102.0], window=3) is None


def test_realized_volatility_series_is_anchored_to_window_end_date():
    dated_closes = [
        DatedClose(date(2026, 1, day), float(100 + day))
        for day in range(1, 6)
    ]

    points = calculate_realized_volatility_series(dated_closes, window=3)

    assert [point.date for point in points] == [date(2026, 1, 4), date(2026, 1, 5)]


def test_previous_day_changes_are_anchored_to_current_date():
    dated_closes = [
        DatedClose(date(2026, 1, 5), 100.0),
        DatedClose(date(2026, 1, 6), 105.0),
        DatedClose(date(2026, 1, 7), 102.0),
    ]

    points = calculate_previous_day_changes(dated_closes)

    assert [(point.date, point.value_percent) for point in points] == [
        (date(2026, 1, 6), pytest.approx(5.0)),
        (date(2026, 1, 7), pytest.approx(-2.857142857)),
    ]


def test_market_volatility_rejects_non_positive_prices_and_invalid_window():
    with pytest.raises(ValueError):
        calculate_realized_volatility([100.0, 0.0], window=1)
    with pytest.raises(ValueError):
        calculate_realized_volatility([100.0, 101.0], window=0)


def test_distribution_summary_calculates_vix_statistics_and_percentiles():
    summary = summarize_distribution([10.0, 20.0, 30.0, 40.0])

    assert summary["count"] == 4
    assert summary["mean"] == pytest.approx(25.0)
    assert summary["median"] == pytest.approx(25.0)
    assert summary["percentiles"]["p25"] == pytest.approx(17.5)
    assert summary["percentiles"]["p95"] == pytest.approx(38.5)


def test_pearson_correlation_matches_identically_increasing_values():
    assert calculate_pearson_correlation([(1.0, 2.0), (2.0, 4.0), (3.0, 6.0)]) == pytest.approx(1.0)


def test_pearson_correlation_returns_none_for_insufficient_or_constant_data():
    assert calculate_pearson_correlation([(1.0, 2.0)]) is None
    assert calculate_pearson_correlation([(1.0, 2.0), (1.0, 3.0)]) is None
