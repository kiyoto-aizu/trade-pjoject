from dataclasses import replace

import pytest

from src.domain.market_regime import MarketRegime, MarketRegimeAssessment, MarketRegimeThresholds
from src.domain.market_tendency import TendencyPeriod, build_market_tendency


THRESHOLDS = MarketRegimeThresholds()


def assessment(
    *,
    regime=MarketRegime.NORMAL,
    realized_volatility=10.0,
    vix=10.0,
    nikkei_change=0.5,
    adx=20.0,
):
    return MarketRegimeAssessment(
        regime=regime,
        realized_volatility_percent=realized_volatility,
        vix=vix,
        nikkei_change_percent=nikkei_change,
        data_available=True,
        adx=adx,
    )


def make_summary(market, ratios=(1.0,), *, period=TendencyPeriod.PREVIOUS_CLOSE):
    return build_market_tendency(
        market,
        ratios,
        period=period,
        activity_label="候補の活発度",
        thresholds=THRESHOLDS,
        adx_threshold=27.0,
        normal_rsi_threshold=55.0,
        caution_rsi_threshold=60.0,
    )


@pytest.mark.parametrize(
    ("adx", "expected"),
    [(26.99, "方向感は出にくく"), (27.0, "方向感は出やすく")],
)
def test_trend_text_uses_existing_adx_boundary(adx, expected):
    assert expected in make_summary(assessment(adx=adx)).tendency_line


@pytest.mark.parametrize(
    ("realized_volatility", "expected"),
    [
        (16.99, "値幅は普通"),
        (17.0, "値幅はやや出やすい"),
        (28.99, "値幅はやや出やすい"),
        (29.0, "値幅が大きく荒れやすい"),
    ],
)
def test_range_text_uses_existing_realized_volatility_boundaries(realized_volatility, expected):
    assert expected in make_summary(assessment(realized_volatility=realized_volatility)).tendency_line


@pytest.mark.parametrize(
    ("nikkei_change", "vix", "expected"),
    [
        (-1.99, 16.99, "パニック的な下げではない"),
        (-2.0, 10.0, "下げに勢いがある"),
        (-0.1, 17.0, "下げに勢いがある"),
        (0.0, 30.0, "地合いは堅調"),
    ],
)
def test_downside_text_reuses_nikkei_and_vix_boundaries(nikkei_change, vix, expected):
    assert expected in make_summary(
        assessment(nikkei_change=nikkei_change, vix=vix)
    ).tendency_line


@pytest.mark.parametrize(
    ("ratios", "expected"),
    [
        ([0.7999], "動きは小さめ"),
        ([0.8], "普通"),
        ([1.1999], "普通"),
        ([1.2], "やや活発"),
    ],
)
def test_activity_text_uses_mean_ratio_boundaries(ratios, expected):
    assert expected in make_summary(assessment(), ratios).activity_line


def test_current_day_action_uses_regime_and_configured_rsi_values():
    caution = make_summary(
        assessment(regime=MarketRegime.CAUTION),
        period=TendencyPeriod.CURRENT_DAY,
    )
    danger = make_summary(
        assessment(regime=MarketRegime.DANGER),
        period=TendencyPeriod.CURRENT_DAY,
    )
    assert caution.market_line == "地合い: やや警戒"
    assert caution.today_action_line == "今日の動き方: 買いに必要なRSIは55→60に厳格化"
    assert danger.today_action_line == "今日の動き方: 新規買いは停止（保有銘柄の売却は継続）"
    assert make_summary(assessment()).today_action_line is None


def test_caution_action_formats_non_default_rsi_thresholds():
    summary = build_market_tendency(
        assessment(regime=MarketRegime.CAUTION),
        [],
        period=TendencyPeriod.CURRENT_DAY,
        activity_label="対象銘柄の活発度",
        thresholds=THRESHOLDS,
        adx_threshold=27.0,
        normal_rsi_threshold=57.5,
        caution_rsi_threshold=62.0,
    )

    assert summary.today_action_line == "今日の動き方: 買いに必要なRSIは57.5→62に厳格化"


def test_activity_missing_values_are_reported_in_summary_without_raising():
    summary = make_summary(assessment(), [None, float("nan"), "invalid"])

    assert "算出できません" in summary.activity_line
    assert summary.activity_sample_count == 0
    assert summary.activity_missing_count == 3


def test_unavailable_market_assessment_is_rejected_for_caller_fallback():
    unavailable = replace(assessment(), data_available=False, failure_reason="missing")

    with pytest.raises(ValueError, match="MarketRegime評価値"):
        make_summary(unavailable)