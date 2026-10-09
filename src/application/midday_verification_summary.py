"""
================================================================================
昼フィルタ 検証用実行の要約
所要時間・取得失敗の率・理由別件数・窓内本数の分布・270円帯の比較・本番の予算に収まるかを集計します。
================================================================================
"""
from statistics import median

from src.application.midday_filtering_usecase import REASON_FETCH_FAILED, REASON_LABELS, MiddayRunReport

BAR_COUNT_BUCKETS = (("0本", 0, 0), ("1〜9本", 1, 9), ("10〜19本", 10, 19), ("20〜29本", 20, 29), ("30本以上", 30, 10**9))


def summarize_verification(report: MiddayRunReport, production_budget_seconds: float) -> dict:
    """検証用実行の結果を、本番の予算(開始〜締切の秒数)と比べて要約する。"""
    outcomes = list(report.outcomes.values())
    fetch_seconds = [o.fetch_ms / 1000 for o in outcomes if o.fetch_ms is not None]
    failed = sum(o.reason_code == REASON_FETCH_FAILED for o in outcomes)
    reasons: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.reason_code:
            reasons[outcome.reason_code] = reasons.get(outcome.reason_code, 0) + 1
    bar_counts = [(o.window or {}).get("bar_count", 0) for o in outcomes if o.reason_code != REASON_FETCH_FAILED]
    distribution = {
        label: sum(low <= count <= high for count in bar_counts) for label, low, high in BAR_COUNT_BUCKETS
    }
    margin = production_budget_seconds - report.elapsed_seconds
    return {
        "unique_symbol_count": report.unique_symbol_count,
        "fetched_count": report.fetched_count,
        "elapsed_seconds": report.elapsed_seconds,
        "per_symbol_median_seconds": median(fetch_seconds) if fetch_seconds else None,
        "per_symbol_max_seconds": max(fetch_seconds) if fetch_seconds else None,
        "fetch_failed_count": failed,
        "fetch_failed_rate": failed / len(outcomes) if outcomes else None,
        "reason_counts": reasons,
        "bar_count_distribution": distribution,
        "comparison": report.comparison,
        "production_budget_seconds": production_budget_seconds,
        "fits_in_production_budget": margin >= 0,
        "margin_seconds": margin,
    }


def format_verification_summary(summary: dict) -> list[str]:
    lines = ["===== 昼フィルタ 検証用実行の要約 ====="]
    lines.append(f"対象{summary['unique_symbol_count']}銘柄 / 取得済み{summary['fetched_count']}銘柄")
    lines.append(f"所要時間(全体): {summary['elapsed_seconds'] / 60:.1f}分({summary['elapsed_seconds']:.0f}秒)")
    if summary["per_symbol_median_seconds"] is not None:
        lines.append(
            f"1銘柄あたり: 中央値{summary['per_symbol_median_seconds']:.1f}秒 / 最大{summary['per_symbol_max_seconds']:.1f}秒"
        )
    if summary["fetch_failed_rate"] is not None:
        lines.append(f"取得失敗: {summary['fetch_failed_count']}件({summary['fetch_failed_rate'] * 100:.1f}%)")
    if summary["reason_counts"]:
        lines.append("理由別の件数: " + ", ".join(
            f"{REASON_LABELS.get(code, code)}={count}" for code, count in sorted(summary["reason_counts"].items())
        ))
    else:
        lines.append("理由別の件数: 評価対象外はありません")
    lines.append("09:00〜09:30の窓内本数: " + ", ".join(f"{k}={v}" for k, v in summary["bar_count_distribution"].items()))
    comparison = summary["comparison"]
    if comparison and comparison.get("available") and comparison.get("stats"):
        stats = comparison["stats"]
        lines.append(
            f"270円帯の推定/実測: 中央値{stats['median']:.2f}(最小{stats['min']:.2f}〜最大{stats['max']:.2f}、"
            f"{stats['count']}件) 上位{comparison['top_n']}の一致{comparison['top_overlap_count']}件"
        )
    else:
        lines.append("270円帯の推定/実測: 比較できませんでした")
    budget_minutes = summary["production_budget_seconds"] / 60
    if summary["fits_in_production_budget"]:
        lines.append(f"本番の予算({budget_minutes:.0f}分)に収まる: はい(余裕{summary['margin_seconds'] / 60:.1f}分)")
    else:
        lines.append(f"本番の予算({budget_minutes:.0f}分)に収まる: いいえ({-summary['margin_seconds'] / 60:.1f}分超過)")
    return lines
