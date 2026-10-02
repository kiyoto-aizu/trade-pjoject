import pytest

from scripts.analysis.analyze_skip_same_day_basis import (
    classify_outcome,
    compare_outcomes,
    minute_same_day_pnl,
    observation_same_day_pnl,
    open_to_close_pnl,
    price_at_or_before,
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
