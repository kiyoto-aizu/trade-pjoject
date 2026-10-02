import math

import pytest

from scripts.analysis.analyze_regime_skip_by_sensitivity import (
    assign_terciles,
    average_turnover,
    correlation_and_beta,
    daily_returns,
    dedupe_symbol_day,
    summarize_group,
)


def _dates(n):
    return [f"2026-06-{day:02d}" for day in range(1, n + 1)]


def test_daily_returns():
    assert daily_returns({"2026-06-01": 100.0, "2026-06-02": 110.0}) == {"2026-06-02": pytest.approx(0.1)}


def test_beta_and_correlation_exact_linear():
    days = _dates(30)
    index = {d: 0.01 * ((i % 5) - 2) for i, d in enumerate(days)}
    stock = {d: 2.0 * r for d, r in index.items()}
    result = correlation_and_beta(stock, index, None, window=None, min_observations=10)
    assert result["beta"] == pytest.approx(2.0)
    assert result["correlation"] == pytest.approx(1.0)
    negative = {d: -0.5 * r for d, r in index.items()}
    assert correlation_and_beta(negative, index, None, None, 10)["correlation"] == pytest.approx(-1.0)


def test_no_lookahead_and_window():
    days = _dates(30)
    index = {d: 0.01 * ((i % 5) - 2) for i, d in enumerate(days)}
    stock = dict(index)
    cutoff = days[20]
    # カットオフ以降を壊しても結果は変わらない
    broken = {d: (r if d < cutoff else 99.0) for d, r in stock.items()}
    base = correlation_and_beta(stock, index, cutoff, window=10, min_observations=5)
    after = correlation_and_beta(broken, index, cutoff, window=10, min_observations=5)
    assert base == after
    assert base["observations"] == 10


def test_insufficient_or_zero_variance_is_unavailable():
    days = _dates(10)
    index = {d: 0.01 * (i % 3) for i, d in enumerate(days)}
    assert correlation_and_beta(index, index, None, 60, 40) is None
    flat = {d: 0.0 for d in days}
    assert correlation_and_beta(flat, index, None, None, 5) is None


def test_assign_terciles():
    labels = assign_terciles({k: v for k, v in zip("abcdef", [6, 5, 4, 3, 2, 1])})
    assert [labels[k] for k in "fedcba"] == ["低", "低", "中", "中", "高", "高"]
    assert assign_terciles({"x": 1.0}) == {"x": "低"}
    assert assign_terciles({}) == {}


def test_average_turnover_uses_only_prior_days():
    days = _dates(5)
    closes = {d: 10.0 for d in days}
    volumes = {d: 100.0 for d in days}
    volumes[days[4]] = 1e9
    assert average_turnover(closes, volumes, days[4], window=4) == pytest.approx(1000.0)
    assert average_turnover(closes, None, days[4], window=4) is None
    assert average_turnover(closes, volumes, days[2], window=4) is None


def test_dedupe_prefers_finalized_and_one_per_symbol_day():
    events = [
        {"id": 1, "symbol": "1", "occurred_at": "2026-09-01T09:00:00", "status": "finalized"},
        {"id": 2, "symbol": "1", "occurred_at": "2026-09-01T10:00:00", "status": "observing"},
        {"id": 3, "symbol": "1", "occurred_at": "2026-09-02T10:00:00", "status": "observing"},
    ]
    result = dedupe_symbol_day(events)
    assert len(result) == 2
    assert {e["id"] for e in result} == {1, 3}


def test_summarize_group():
    events = [
        {"status": "finalized", "outcome": "損失回避の可能性", "hypothetical_pnl_before_cost": -100.0},
        {"status": "finalized", "outcome": "利益取り逃しの可能性", "hypothetical_pnl_before_cost": 300.0},
        {"status": "finalized", "outcome": "利益取り逃しの可能性", "hypothetical_pnl_before_cost": 100.0},
        {"status": "observing", "outcome": None, "hypothetical_pnl_before_cost": None},
    ]
    s = summarize_group(events)
    assert (s["n"], s["loss_avoided"], s["missed"], s["pending"]) == (4, 1, 2, 1)
    assert s["missed_ratio"] == pytest.approx(2 / 3)
    assert s["pnl_sum"] == 300.0 and s["pnl_median"] == 100.0
    assert summarize_group([])["missed_ratio"] is None
    assert not math.isnan(s["missed_ratio"])
