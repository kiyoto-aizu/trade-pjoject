"""通知向けの市場傾向・活発度の文言を生成する純粋関数。"""
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from statistics import fmean
from typing import Sequence

from src.domain.market_regime import MarketRegime, MarketRegimeAssessment, MarketRegimeThresholds


class TendencyPeriod(str, Enum):
    PREVIOUS_CLOSE = "前日終値ベース"
    CURRENT_DAY = "当日"


ACTIVITY_QUIET_BOUNDARY = 0.8
ACTIVITY_ACTIVE_BOUNDARY = 1.2

REGIME_LABELS = {
    MarketRegime.NORMAL: "通常",
    MarketRegime.CAUTION: "やや警戒",
    MarketRegime.DANGER: "危険・新規買い停止",
}
TREND_TEXT = {
    "weak": "方向感は出にくく",
    "strong": "方向感は出やすく",
}
RANGE_TEXT = {
    "normal": "値幅は普通の",
    "caution": "値幅はやや出やすい",
    "danger": "値幅が大きく荒れやすい",
}
DOWNSIDE_TEXT = {
    "contained": "パニック的な下げではない",
    "strong": "下げに勢いがある。慎重に",
    "up": "地合いは堅調になりやすい",
    "large_up": "上昇幅が大きく、振れが出やすい",
}
ACTIVITY_TEXT = {
    "quiet": "動きは小さめ",
    "ordinary": "普通",
    "active": "やや活発",
}
TODAY_ACTION_TEXT = {
    MarketRegime.NORMAL: "通常ルールで運用",
    MarketRegime.CAUTION: "買いに必要なRSIは{normal}→{caution}に厳格化",
    MarketRegime.DANGER: "新規買いは停止（保有銘柄の売却は継続）",
}


@dataclass(frozen=True)
class MarketTendencySummary:
    market_line: str
    tendency_line: str
    activity_line: str
    today_action_line: str | None
    activity_sample_count: int
    activity_missing_count: int


def build_market_tendency(
    assessment: MarketRegimeAssessment,
    turnover_ratios: Sequence[float | None],
    *,
    period: TendencyPeriod,
    activity_label: str,
    thresholds: MarketRegimeThresholds,
    adx_threshold: float,
    normal_rsi_threshold: float,
    caution_rsi_threshold: float,
) -> MarketTendencySummary:
    """既存の市場判定値と売買代金比から通知行を組み立てます。"""
    values = (
        assessment.realized_volatility_percent,
        assessment.vix,
        assessment.nikkei_change_percent,
        assessment.adx,
    )
    if not assessment.data_available or any(value is None for value in values):
        raise ValueError("市場傾向に必要なMarketRegime評価値がありません")

    realized_volatility, vix, nikkei_change, adx = values
    if any(not isfinite(value) for value in values):
        raise ValueError("市場傾向に必要なMarketRegime評価値が有限値ではありません")

    trend_text = TREND_TEXT["strong" if adx >= adx_threshold else "weak"]
    if realized_volatility >= thresholds.realized_vol_danger:
        range_text = RANGE_TEXT["danger"]
    elif realized_volatility >= thresholds.realized_vol_caution:
        range_text = RANGE_TEXT["caution"]
    else:
        range_text = RANGE_TEXT["normal"]

    if nikkei_change >= thresholds.nikkei_change_upgrade:
        downside_text = DOWNSIDE_TEXT["large_up"]
    elif nikkei_change >= 0:
        downside_text = DOWNSIDE_TEXT["up"]
    elif (
        abs(nikkei_change) < thresholds.nikkei_change_upgrade
        and vix < thresholds.vix_caution
    ):
        downside_text = DOWNSIDE_TEXT["contained"]
    else:
        downside_text = DOWNSIDE_TEXT["strong"]

    ratios = []
    for ratio in turnover_ratios:
        try:
            numeric_ratio = float(ratio) if ratio is not None else None
        except (TypeError, ValueError, OverflowError):
            numeric_ratio = None
        if numeric_ratio is not None and isfinite(numeric_ratio) and numeric_ratio >= 0:
            ratios.append(numeric_ratio)
    missing_count = len(turnover_ratios) - len(ratios)
    if ratios:
        average_ratio = fmean(ratios)
        maximum_ratio = max(ratios)
        if average_ratio < ACTIVITY_QUIET_BOUNDARY:
            activity_text = ACTIVITY_TEXT["quiet"]
        elif average_ratio < ACTIVITY_ACTIVE_BOUNDARY:
            activity_text = ACTIVITY_TEXT["ordinary"]
        else:
            activity_text = ACTIVITY_TEXT["active"]
        activity_detail = (
            f"20日平均売買代金比 最大{maximum_ratio:.1f}倍・"
            f"平均{average_ratio:.1f}倍({activity_text})"
        )
    else:
        activity_detail = "20日平均売買代金比を算出できません"

    market_prefix = "地合い" if period == TendencyPeriod.CURRENT_DAY else "地合い(前日終値ベース)"
    today_action = None
    if period == TendencyPeriod.CURRENT_DAY:
        action = TODAY_ACTION_TEXT[assessment.regime].format(
            normal=f"{normal_rsi_threshold:g}",
            caution=f"{caution_rsi_threshold:g}",
        )
        today_action = f"今日の動き方: {action}"

    return MarketTendencySummary(
        market_line=f"{market_prefix}: {REGIME_LABELS[assessment.regime]}",
        tendency_line=(
            f"傾向(前日終値ベース): {trend_text}、{range_text}地合い。{downside_text}。"
        ),
        activity_line=f"{activity_label}: {activity_detail}",
        today_action_line=today_action,
        activity_sample_count=len(ratios),
        activity_missing_count=missing_count,
    )