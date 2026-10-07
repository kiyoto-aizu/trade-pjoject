"""日次・週次・月次の通知で共有する実績・答え合わせの表示。"""
from __future__ import annotations

from collections import Counter
from datetime import date

from src.application.trend_check_report import aggregate
from src.application.price_band_trend_check import (
    price_band_has_reference,
    price_band_missing_bands,
    price_band_monthly_lines,
    price_band_rate_line,
)
from src.domain.trend_check import JUDGED_LABELS, LABEL_TREND
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")
REFERENCE_NOTE = "※参考値: 10件未満、または総数30件未満の帯の割合は参考値です。"
SKIP_FIELDS = {
    "ATR危険度見送り": "atr_danger_skips",
    "市場危険度見送り": "market_regime_danger_skips",
    "注意レジームRSI見送り": "market_regime_caution_rsi_filters",
}


def _percent(trend: int, judged: int) -> str:
    if not judged:
        return "判定可能な件数なし"
    return f"{trend}/{judged}件 ({trend / judged:.1%})"


def _is_reference(judged: int) -> bool:
    return 0 < judged < 10


def load_trend_check(
    repository: TrendCheckRepository,
    start: date,
    end: date,
    *,
    with_progression: bool = False,
) -> dict | None:
    versions = repository.versions_with_rows(start.isoformat(), end.isoformat())
    if not versions:
        return None
    latest = repository.latest_version()
    version = latest["version"] if latest and latest["version"] in versions else versions[-1]
    summaries = repository.load_summaries(version, start.isoformat(), end.isoformat())
    rows = repository.load_rows(version, start.isoformat(), end.isoformat())
    if not summaries:
        return None
    return {
        "version": version,
        "aggregate": aggregate(rows, summaries, with_progression=with_progression),
        "summaries": summaries,
        "rows": rows,
    }


def trend_check_lines(result: dict | None, *, include_progression: bool = False) -> list[str]:
    if result is None:
        return ["答え合わせ: なし（期間内の日次判定結果なし）"]

    agg = result["aggregate"]
    bought = agg["bought"]
    bought_undecidable = sum(
        1 for row in result["rows"]
        if row["in_selected"] and row["bought"] and row["label"] not in JUDGED_LABELS
    )
    lines = [
        f"判定基準: {result['version']}",
        f"買った銘柄 {bought['bought_count']}件: "
        f"{_percent(bought['stat']['trend'], bought['stat']['n'])} "
        f"(判定不能 {bought_undecidable}件)",
        "選定銘柄: "
        f"{_percent(agg['pooled_selected']['trend'], agg['pooled_selected']['n'])} "
        f"(判定不能 {agg['undecidable_selected']}件)",
        "候補全体: "
        f"{_percent(agg['pooled_candidate']['trend'], agg['pooled_candidate']['n'])} "
        f"(判定不能 {agg['undecidable_candidate']}件)",
    ]
    sources = agg["universe_sources"]
    if "daily_cache_all" in sources:
        lines.append("注記: 候補全体は日足キャッシュ全銘柄による代用を含みます。")
    if "screening_previous_day" in sources:
        lines.append("注記: 候補全体は前営業日のスクリーニング結果による代用を含みます。")

    if include_progression:
        lines.append("週内推移:")
        for item in agg["daily"]:
            lines.append(
                f"- {item['date']}: 選定 {_percent(item['selected_trend'], item['selected_judged'])}, "
                f"候補 {_percent(item.get('candidate_trend', 0), _summary_candidate_judged(result, item['date']))}"
            )
    return lines


def trend_check_has_reference(result: dict | None, *, include_progression: bool = False) -> bool:
    """trend_check_linesで表示した割合に、件数不足(10件未満)のものがあるか。"""
    if result is None:
        return False
    agg = result["aggregate"]
    judged = [
        agg["bought"]["stat"]["n"],
        agg["pooled_selected"]["n"],
        agg["pooled_candidate"]["n"],
    ]
    if include_progression:
        for item in agg["daily"]:
            judged.append(item["selected_judged"])
            judged.append(_summary_candidate_judged(result, item["date"]))
    return any(_is_reference(int(value)) for value in judged)


def _summary_candidate_judged(result: dict, day: str) -> int:
    summary = next((item for item in result["summaries"] if item["trade_date"] == day), None)
    return int(summary["candidate_judged"]) if summary else 0


def _skip_counts(report: dict) -> dict[str, int]:
    return {
        label: len(report.get(field) or [])
        for label, field in SKIP_FIELDS.items()
        if report.get(field)
    }


def _skip_summary(report: dict) -> str:
    """0件と未記録(古いレポートで項目自体がない)を区別して表示する。"""
    if any(field not in report for field in SKIP_FIELDS.values()):
        return "見送り 未記録"
    skips = _skip_counts(report)
    if not skips:
        return "見送り 0件"
    detail = "・".join(f"{key} {value}" for key, value in skips.items())
    return f"見送り {sum(skips.values())}件（{detail}）"


def _nikkei_text(market: dict) -> str:
    change = market.get("nikkei_change_percent")
    return f"日経 {change:+.2f}%" if isinstance(change, (int, float)) else "日経 不明"


def daily_paper_lines(report: dict | None) -> list[str]:
    if report is None:
        return ["日次実績: 日次レポートなし"]
    market = report.get("market_conditions") or {}
    positions = report.get("positions") or []
    errors = report.get("log_errors") or {}
    error_count = int(errors.get("count") or 0)
    error_text = f"エラー {error_count}件"
    if error_count:
        summaries = " / ".join(str(item) for item in (errors.get("summaries") or [])[:2])
        error_text += f"（{summaries}）" if summaries else ""
    regime = market.get("regime") or market.get("assessment_status") or "不明"
    return [
        f"損益 実現 {float(report.get('realized_profit_loss') or 0):+.0f}円・"
        f"評価 {float(report.get('unrealized_profit_loss') or 0):+.0f}円 / "
        f"約定 {int(report.get('order_count') or 0)}件 / 保有 {len(positions)}銘柄",
        f"市場状態: {regime}（{_nikkei_text(market)}）",
        f"{_skip_summary(report)} / {error_text}",
    ]


def period_paper_lines(summary: dict, kind: str) -> list[str]:
    daily = summary["daily"]
    ops = daily["operational_summary"]
    reports = daily["reports"]
    lines = [
        f"ペーパートレード: 損益 {daily['total_profit_loss']:+.0f}円 / "
        f"約定 {daily['order_count']}件 / 日次レポート {daily['report_count']}日",
        f"市場状態: {ops['market_assessment_status_counts']}",
        f"エラー・データ抜け: エラー {ops['log_error_count']}件 / "
        f"緊急停止 {ops['emergency_stop_days']}日",
    ]
    skip_totals: dict[str, int] = {}
    for report in reports:
        for label, count in report.get("skip_counts", {}).items():
            skip_totals[label] = skip_totals.get(label, 0) + count
    lines.append(
        "見送り内訳: "
        + (", ".join(f"{key} {value}件" for key, value in sorted(skip_totals.items())) if skip_totals else "記録なし")
    )

    if kind == "週次":
        by_weekday: dict[int, list[float]] = {}
        for report in reports:
            day = date.fromisoformat(str(report["date"])[:10])
            by_weekday.setdefault(day.weekday(), []).append(float(report.get("total_profit_loss") or 0))
        lines.append(
            "曜日別損益: "
            + (", ".join(
                f"{WEEKDAYS_JA[weekday]} {sum(values):+.0f}円"
                for weekday, values in sorted(by_weekday.items())
            ) if by_weekday else "記録なし")
        )
        lines.append("市場状態の推移: " + " → ".join(
            f"{str(item['date'])[-5:]} {item.get('market_regime') or item.get('market_assessment_status') or '不明'}"
            for item in reports
        ) if reports else "市場状態の推移: 記録なし")
    return lines


def period_notification_lines(summary: dict, trend: dict | None, kind: str, analysis: str | None) -> list[str]:
    backtest = summary["backtest"]
    backtest_line = (
        f"対象期間一致: 損益 {backtest['total_pnl']}円 / 約定 {backtest['total_trades']}件"
        if backtest["exact_period_run_available"]
        else "なし（対象期間に一致するバックテスト結果なし）"
    )
    lines = [
        f"A. ペーパートレード実績 ({kind})",
        *period_paper_lines(summary, kind),
        "B. 戦略の答え合わせ",
        *trend_check_lines(trend, include_progression=kind == "週次"),
    ]
    price_band_trend = summary.get("price_band_trend_check")
    if kind == "月次":
        lines.extend(price_band_monthly_lines(price_band_trend))
    else:
        lines.append(price_band_rate_line(price_band_trend))
    if kind == "月次" and trend is None:
        lines.append("判定できなかった日: 日次判定結果なし（原因を特定できる保存データなし）")
    elif kind == "月次" and trend is not None:
        undecidable_days = [
            item for item in trend["summaries"] if item["selected_undecidable"] or item["candidate_undecidable"]
        ]
        if undecidable_days:
            reason_counts = Counter(
                row.get("undecidable_reason") or "理由記録なし"
                for row in trend["rows"]
                if row["label"] not in JUDGED_LABELS
            )
            lines.append("判定できなかった日: " + ", ".join(
                f"{item['trade_date']} (選定{item['selected_undecidable']}件/候補{item['candidate_undecidable']}件)"
                for item in undecidable_days
            ))
            if reason_counts:
                lines.append("判定不能の原因: " + ", ".join(
                    f"{reason} {count}件" for reason, count in sorted(reason_counts.items())
                ))
        else:
            lines.append("判定できなかった日: なし")
    if trend_check_has_reference(trend, include_progression=kind == "週次") or price_band_has_reference(
        price_band_trend
    ):
        lines.append(REFERENCE_NOTE)
    lines.extend([
        "C. バックテスト（別枠）",
        backtest_line,
        "D. 次回確認",
        *next_check_lines(trend, price_band_trend),
    ])
    if analysis:
        lines.extend(["LLM評価（参考）:", analysis])
    return lines


def next_check_lines(
    trend: dict | None,
    price_band_trend: dict | None,
    *,
    cache_update_ok: bool = True,
    check_ok: bool = True,
) -> list[str]:
    """実際に確認が必要な点だけを返す。何もなければ「なし」。"""
    items: list[str] = []
    if not cache_update_ok:
        items.append("日足更新が完了していません（答え合わせは判定不能）")
    elif not check_ok:
        items.append("答え合わせ処理でエラーが発生しました（ログを確認）")
    elif trend is not None:
        agg = trend["aggregate"]
        selected, candidate = agg["undecidable_selected"], agg["undecidable_candidate"]
        if selected or candidate:
            items.append(f"判定不能 選定{selected}件/候補{candidate}件（原因を確認）")
    for band, reason in price_band_missing_bands(price_band_trend):
        items.append(f"{band}円帯の欠測: {reason}")
    return items or ["なし"]


def _missing_band_sentence(price_band_trend: dict | None) -> str:
    bands = [band for band, _ in price_band_missing_bands(price_band_trend)]
    return "・".join(f"{band}円" for band in bands) + "帯は欠測です。" if bands else ""


def daily_conclusion(
    report: dict | None,
    *,
    cache_update_ok: bool,
    price_band_trend: dict | None = None,
) -> str:
    """日次分析の【概要】。結論を1〜2行で示す。"""
    head = (
        "日足更新・答え合わせ・日次レビューが完了しました。"
        if cache_update_ok
        else "日足更新が完了せず、答え合わせは判定不能です。"
    )
    if report is None:
        trade = "日次レポートなし。"
    elif not int(report.get("order_count") or 0):
        trade = "今日は売買なし。"
    else:
        trade = (
            f"今日は約定{int(report.get('order_count') or 0)}件、"
            f"損益 実現{float(report.get('realized_profit_loss') or 0):+.0f}円・"
            f"評価{float(report.get('unrealized_profit_loss') or 0):+.0f}円。"
        )
    return head + trade + _missing_band_sentence(price_band_trend)


def period_conclusion(summary: dict, kind: str) -> str:
    """週次・月次分析の【概要】。固有の集計内容は変えず、結論だけを先頭に示す。"""
    daily = summary["daily"]
    span = "今週" if kind == "週次" else "今月"
    if not daily["report_count"]:
        trade = f"{span}は日次レポートなし。"
    else:
        trade = f"{span}は約定{daily['order_count']}件、損益{daily['total_profit_loss']:+.0f}円。"
    return (
        f"{kind}分析が完了しました。" + trade
        + _missing_band_sentence(summary.get("price_band_trend_check"))
    )


def daily_notification_lines(
    report: dict | None,
    trend: dict | None,
    *,
    cache_update_ok: bool,
    analysis: str | None,
    price_band_trend: dict | None = None,
    check_ok: bool = True,
) -> list[str]:
    lines = [
        "A. ペーパートレード実績",
        *daily_paper_lines(report),
        "B. 戦略の答え合わせ",
    ]
    if not cache_update_ok:
        lines.append("判定不能(日足更新失敗)")
    else:
        lines.extend(trend_check_lines(trend))
    lines.append(price_band_rate_line(price_band_trend))
    if (cache_update_ok and trend_check_has_reference(trend)) or price_band_has_reference(price_band_trend):
        lines.append(REFERENCE_NOTE)
    lines.append("D. 次回確認")
    lines.extend(
        next_check_lines(trend, price_band_trend, cache_update_ok=cache_update_ok, check_ok=check_ok)
    )
    if analysis:
        lines.extend(["LLM日次評価（参考）:", analysis])
    return lines
