"""市場傾向文を通知へ安全に追加するアプリケーション補助関数。"""
import logging
from typing import Sequence

from src.config import config
from src.domain.market_regime import MarketRegimeAssessment
from src.domain.market_tendency import TendencyPeriod, build_market_tendency

logger = logging.getLogger(__name__)


def build_market_tendency_lines(
    assessment: MarketRegimeAssessment | None,
    turnover_ratios: Sequence[float | None] | None,
    *,
    period: TendencyPeriod,
    activity_label: str,
    include_market_line: bool = True,
    include_tendency_line: bool = True,
    include_activity_line: bool = True,
) -> list[str]:
    """生成失敗時は空行を返し、既存通知を維持しながら理由を記録します。"""
    if assessment is None or not assessment.data_available:
        reason = getattr(assessment, "failure_reason", None) or "MarketRegime評価値なし"
        logger.warning("TENDENCY_MARKET_UNAVAILABLE: %s", reason)
        logger.warning("TENDENCY_ACTIVITY_UNAVAILABLE: 対象=%s | 理由=市場評価が利用できません", activity_label)
        return []

    ratios = list(turnover_ratios or [])
    try:
        summary = build_market_tendency(
            assessment,
            ratios,
            period=period,
            activity_label=activity_label,
            thresholds=config.MARKET_REGIME_THRESHOLDS,
            adx_threshold=config.MARKET_REGIME_ADX_TREND_THRESHOLD,
            normal_rsi_threshold=config.RSI_ENTRY_THRESHOLD,
            caution_rsi_threshold=config.RSI_ENTRY_THRESHOLD_CAUTION,
        )
    except Exception:
        logger.exception("TENDENCY_GENERATION_FAILED: 傾向文を追加できません")
        return []

    if summary.activity_sample_count == 0:
        logger.warning("TENDENCY_ACTIVITY_UNAVAILABLE: 対象=%s | 理由=有効な売買代金比なし", activity_label)
    elif summary.activity_missing_count:
        logger.warning(
            "TENDENCY_ACTIVITY_UNAVAILABLE: 対象=%s | 有効=%d | 欠損=%d",
            activity_label,
            summary.activity_sample_count,
            summary.activity_missing_count,
        )

    lines = []
    if include_market_line:
        lines.append(summary.market_line)
    if include_tendency_line:
        lines.append(summary.tendency_line)
    if include_activity_line:
        lines.append(summary.activity_line)
    if summary.today_action_line is not None:
        lines.append(summary.today_action_line)
    return lines