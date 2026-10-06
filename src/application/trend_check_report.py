"""トレンド答え合わせの週次・月次集計とレポート(md/CSV)。

週次・月次は日次の保存結果(SQLite)を積み上げるだけで、日足や分足からは再計算しない。
集計は同じ版の行だけで行い、版が違う行は混ぜない。
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

from src.application.trend_check_usecase import UNIVERSE_DAILY_CACHE, UNIVERSE_DESCRIPTIONS
from src.domain.trend_check import JUDGED_LABELS, LABEL_TREND
from src.infrastructure.persistence.trend_check_repository import RESULT_COLUMNS, TrendCheckRepository

MIN_CATEGORY_N = 10  # これ未満の区分は参考値
MIN_TOTAL_N = 30  # これ未満の総数は傾向の参考のみ
SAME_LEVEL_PT = 3.0  # 差がこの範囲内なら「同程度」
TREND_SLOPE_HALF_PT = 5.0

FEATURES: dict[str, tuple[str, list[float], list[str]]] = {
    "turnover_ratio": ("売買代金倍率", [0.5, 1.0], ["<0.5", "0.5-1.0", ">=1.0"]),
    "price": ("価格(円)", [50, 100, 200], ["<50", "50-100", "100-200", ">=200"]),
    "rsi": ("RSI", [40, 60], ["<40", "40-60", ">=60"]),
    "atr_pct": ("ATR/価格(%)", [3, 6], ["<3%", "3-6%", ">=6%"]),
}
UNKNOWN = "不明"


def pct(value: Optional[float], digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def num(value: Optional[float], digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _stat(rows: list[dict]) -> dict:
    judged = [row for row in rows if row["label"] in JUDGED_LABELS]
    trend = sum(1 for row in judged if row["label"] == LABEL_TREND)
    return {
        "n": len(judged), "trend": trend, "rate": trend / len(judged) if judged else None,
        "undecidable": len(rows) - len(judged),
    }


def _stat_text(stat: dict) -> str:
    text = f"{stat['trend']}/{stat['n']}件 ({pct(stat['rate'])})"
    return text + (" ※参考値" if stat["n"] < MIN_CATEGORY_N else "")


def _bucket(value: Optional[float], edges: list[float], labels: list[str]) -> str:
    if value is None:
        return UNKNOWN
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


def _feature_value(row: dict, feature: str) -> Optional[float]:
    if feature == "atr_pct":
        if row.get("atr") is None or not row.get("price"):
            return None
        return row["atr"] / row["price"] * 100.0
    return row.get(feature)


def _trend_direction(daily: list[dict]) -> dict:
    rates = [day["selected_rate"] for day in daily if day["selected_rate"] is not None]
    if len(rates) < 4:
        return {"direction": "判断不可(営業日が少ない)", "first_half": None, "second_half": None, "slope_pt_per_day": None}
    half = len(rates) // 2
    first, second = mean(rates[:half]), mean(rates[len(rates) - half:])
    mean_x, mean_y = (len(rates) - 1) / 2, mean(rates)
    denominator = sum((index - mean_x) ** 2 for index in range(len(rates)))
    slope = sum((index - mean_x) * (rate - mean_y) for index, rate in enumerate(rates)) / denominator
    difference = (second - first) * 100
    direction = "上向き" if difference >= TREND_SLOPE_HALF_PT else "下向き" if difference <= -TREND_SLOPE_HALF_PT else "横ばい"
    return {"direction": direction, "first_half": first, "second_half": second, "slope_pt_per_day": slope * 100}


def aggregate(rows: list[dict], summaries: list[dict], with_progression: bool = False) -> dict:
    """同じ版の日次行・日次サマリから期間集計を作る。"""
    selected = [row for row in rows if row["in_selected"]]
    not_selected = [row for row in rows if not row["in_selected"]]
    daily = [
        {
            "date": summary["trade_date"], "selected_rate": summary["selected_rate"],
            "candidate_rate": summary["candidate_rate"], "selected_judged": summary["selected_judged"],
            "selected_trend": summary["selected_trend"], "regime": summary["regime"],
            "universe_source": summary["universe_source"], "selected_undecidable": summary["selected_undecidable"],
        }
        for summary in summaries
    ]
    selected_rates = [day["selected_rate"] for day in daily if day["selected_rate"] is not None]
    candidate_rates = [day["candidate_rate"] for day in daily if day["candidate_rate"] is not None]
    pooled_selected, pooled_candidate = _stat(selected), _stat(rows)

    by_regime = []
    for regime in sorted({row["regime"] or UNKNOWN for row in rows}):
        in_regime = [row for row in rows if (row["regime"] or UNKNOWN) == regime]
        by_regime.append({
            "key": regime, "selected": _stat([row for row in in_regime if row["in_selected"]]),
            "candidate": _stat(in_regime),
            "days": len({row["trade_date"] for row in in_regime}),
        })

    by_feature = {}
    for feature, (title, edges, labels) in FEATURES.items():
        buckets = []
        for label in [*labels, UNKNOWN]:
            in_bucket = [row for row in rows if _bucket(_feature_value(row, feature), edges, labels) == label]
            if not in_bucket:
                continue
            buckets.append({
                "key": label, "selected": _stat([row for row in in_bucket if row["in_selected"]]),
                "candidate": _stat(in_bucket),
            })
        by_feature[feature] = {"title": title, "buckets": buckets}

    bought = [row for row in selected if row["bought"]]
    known = [row for row in selected if row["bought"] is not None]
    bought_judged = [row for row in bought if row["label"] in JUDGED_LABELS]
    pnls = [row["pnl"] for row in bought if row["pnl"] is not None]
    trend_pnls = [row["pnl"] for row in bought if row["pnl"] is not None and row["label"] == LABEL_TREND]
    other_pnls = [row["pnl"] for row in bought if row["pnl"] is not None and row["label"] != LABEL_TREND]
    bought_info = {
        "known_selected_rows": len(known), "unknown_selected_rows": len(selected) - len(known),
        "bought_count": len(bought), "stat": _stat(bought_judged),
        "pnl_count": len(pnls), "pnl_total": sum(pnls) if pnls else None,
        "pnl_average": mean(pnls) if pnls else None,
        "win_count": sum(1 for value in pnls if value > 0),
        "trend_pnl_total": sum(trend_pnls) if trend_pnls else None, "trend_pnl_count": len(trend_pnls),
        "other_pnl_total": sum(other_pnls) if other_pnls else None, "other_pnl_count": len(other_pnls),
    }

    result = {
        "daily": daily,
        "days": len(daily),
        "mean_selected_rate": mean(selected_rates) if selected_rates else None,
        "stdev_selected_rate": stdev(selected_rates) if len(selected_rates) >= 2 else None,
        "mean_candidate_rate": mean(candidate_rates) if candidate_rates else None,
        "pooled_selected": pooled_selected, "pooled_candidate": pooled_candidate,
        "pooled_not_selected": _stat(not_selected),
        "by_regime": by_regime, "by_feature": by_feature, "bought": bought_info,
        "undecidable_selected": pooled_selected["undecidable"],
        "undecidable_candidate": pooled_candidate["undecidable"],
        "undecidable_dates": [day["date"] for day in daily if day["selected_undecidable"]],
        "universe_sources": sorted({day["universe_source"] for day in daily}),
    }
    if with_progression:
        result["progression"] = _trend_direction(daily)
    return result


def conclusion_lines(agg: dict) -> list[str]:
    selected, candidate = agg["pooled_selected"], agg["pooled_candidate"]
    if not selected["n"] or not candidate["n"]:
        return ["判定できた件数がなく、選定がトレンド銘柄を拾えているかは判断できません。"]
    gap = (selected["rate"] - candidate["rate"]) * 100
    wording = "多く" if gap > SAME_LEVEL_PT else "少なく" if gap < -SAME_LEVEL_PT else "同程度に"
    lines = [
        f"選定のトレンド銘柄: {selected['trend']}/{selected['n']}件 ({pct(selected['rate'])}) / "
        f"候補全体(選定を含む): {candidate['trend']}/{candidate['n']}件 ({pct(candidate['rate'])}) / 差 {gap:+.1f}pt",
        f"→ 選定は母集団よりトレンド銘柄を**{wording}**拾えている傾向です(差{SAME_LEVEL_PT:.0f}pt以内は「同程度」とみなす)。",
    ]
    if selected["n"] < MIN_TOTAL_N:
        lines.append(f"※選定の判定件数が{selected['n']}件(目安{MIN_TOTAL_N}件未満)のため参考値です。断定はできません。")
    else:
        lines.append("※件数はまだ限られ、期間内の相場環境にも左右されるため、断定ではなく傾向として扱ってください。")
    if agg["undecidable_selected"]:
        lines.append(f"※選定のうち判定不能が{agg['undecidable_selected']}件あり、上の件数には含めていません。")
    return lines


def compare_with_other_versions(
    repository: TrendCheckRepository, primary: str, start: str, end: str
) -> list[dict]:
    """最新版と旧版を、両方に結果がある日だけで比べる。基準変更による変動と選定の質の変化を見分ける用。"""
    primary_dates = {s["trade_date"] for s in repository.load_summaries(primary, start, end)}
    comparisons = []
    for version in repository.versions_with_rows(start, end):
        other_dates = {s["trade_date"] for s in repository.load_summaries(version, start, end)}
        common = sorted(primary_dates & other_dates)
        if not common:
            continue
        entry = {"version": version, "days": len(common)}
        for key, name in ((primary, "primary"), (version, "version_stats")):
            rows = [row for row in repository.load_rows(key, common[0], common[-1]) if row["trade_date"] in common]
            entry[name] = {"selected": _stat([row for row in rows if row["in_selected"]]), "candidate": _stat(rows)}
        comparisons.append(entry)
    return comparisons


def _definition_block(version_info: dict) -> list[str]:
    return [
        f"## 判定基準 {version_info['version']}",
        f"- 開始日: {version_info['start_date']} / 変更理由: {version_info['reason']}",
        "```json",
        json.dumps(version_info["definition"], ensure_ascii=False, indent=2),
        "```",
        "",
    ]


# ---------------------------------------------------------------- 日次

def build_daily_report(repository: TrendCheckRepository, trade_date: str, version: Optional[str] = None) -> Optional[dict]:
    versions = repository.versions_with_rows(trade_date, trade_date)
    if not versions:
        return None
    version = version if version in versions else versions[-1]
    return {
        "version": version,
        "version_info": repository.get_version(version),
        "rows": repository.load_rows(version, trade_date, trade_date),
        "summary": repository.load_summaries(version, trade_date, trade_date)[0],
        "comparisons": compare_with_other_versions(repository, version, trade_date, trade_date),
    }


def _render_comparison(primary: str, comparisons: list[dict]) -> list[str]:
    others = [item for item in comparisons if item["version"] != primary]
    if not others:
        return ["旧版の結果はありません(比較なし)。"]

    def cell(block: dict) -> str:
        selected, candidate = block["selected"]["rate"], block["candidate"]["rate"]
        gap = f"{(selected - candidate) * 100:+.1f}pt" if selected is not None and candidate is not None else "-"
        return f"{pct(selected)} / {pct(candidate)} / {gap}"

    lines = [
        f"| 比較版 | 共通日数 | 比較版(選定/候補全体/差) | {primary}(選定/候補全体/差) |",
        "|---|---|---|---|",
    ]
    lines += [f"| {item['version']} | {item['days']} | {cell(item['version_stats'])} | {cell(item['primary'])} |" for item in others]
    lines += [
        "",
        "読み方: 選定・候補全体が同じ向きに動き差(選定-候補全体)が変わらなければ基準変更の影響、"
        "差が変わっていれば選定の質の変化の可能性(件数が少ないうちは参考)。",
    ]
    return lines


def render_daily_markdown(report: dict, trade_date: str) -> str:
    summary, rows = report["summary"], report["rows"]
    selected = [row for row in rows if row["in_selected"]]
    lines = [
        f"# トレンド答え合わせ(日次) {trade_date}",
        "",
        f"- 判定に使った版: **{report['version']}** (最新版を主表示)",
        f"- 候補全体の出所: {UNIVERSE_DESCRIPTIONS.get(summary['universe_source'], summary['universe_source'])}",
        f"- 市場レジーム: {summary['regime'] or UNKNOWN}",
        "",
        *_definition_block(report["version_info"]),
        "## 日次サマリ",
        "| 区分 | 銘柄数 | 判定できた数 | トレンド | トレンド割合 | 判定不能 |",
        "|---|---|---|---|---|---|",
        f"| 選定 | {summary['selected_count']} | {summary['selected_judged']} | {summary['selected_trend']} | "
        f"{pct(summary['selected_rate'])} | {summary['selected_undecidable']} |",
        f"| 候補全体 | {summary['candidate_count']} | {summary['candidate_judged']} | {summary['candidate_trend']} | "
        f"{pct(summary['candidate_rate'])} | {summary['candidate_undecidable']} |",
        "",
        f"- 買った銘柄: {summary['bought_count']}件(うちトレンド {summary['bought_trend']}件) / "
        f"損益合計(手数料前・決済済みのみ): {num(summary['pnl_total'])}円",
        "",
    ]
    if summary["selected_undecidable"] or summary["candidate_undecidable"]:
        reasons = sorted({row["undecidable_reason"] for row in rows if row["undecidable_reason"]})
        lines += [
            f"> 判定不能あり({', '.join(reasons)})。日足キャッシュが未更新の場合は、更新後に同じ日を再実行すると上書きされます。",
            "",
        ]
    lines += ["## 旧版との差", *_render_comparison(report["version"], report["comparisons"]), ""]
    lines += [
        "## 選定銘柄の一覧",
        "| 銘柄 | 始値 | 高値 | 安値 | 終値 | 騰落率 | 終値位置 | レンジ/ATR | 判定 | 売買代金倍率 | 価格 | RSI | ATR | "
        "方向一貫性* | VWAP上*比率 | 買った | 損益 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in selected:
        bought = "不明" if row["bought"] is None else "買った" if row["bought"] else "買わず"
        change = "-" if row["open_to_close_pct"] is None else f"{row['open_to_close_pct']:+.2f}%"
        lines.append(
            f"| {row['symbol']} | {num(row['open'], 1)} | {num(row['high'], 1)} | {num(row['low'], 1)} | "
            f"{num(row['close'], 1)} | {change} | {num(row['close_position'])} | {num(row['range_to_atr'])} | "
            f"{row['label']} | {num(row['turnover_ratio'])} | {num(row['price'], 1)} | {num(row['rsi'], 1)} | "
            f"{num(row['atr'])} | {pct(row['direction_consistency'], 0)} | {pct(row['vwap_above_ratio'], 0)} | "
            f"{bought} | {num(row['pnl'])} |"
        )
    lines += [
        "",
        "*分足がある日のみ。補助情報で、v1の判定には使っていません。",
        "RSI・ATRは当日より前の確定日足から計算(選定時点の値とは異なる場合あり)。価格は選定時の板価格(無ければ前日終値)。"
        "売買代金倍率はフィルタ診断が残っている日のみ。",
        "",
        "## 限界",
        "- 1日・10銘柄前後のため、この結果だけで戦略の妥当性は判断できません。週次・月次の積み上げで傾向を見てください。",
    ]
    return "\n".join(lines) + "\n"


def write_csv(path: Path, rows: list[dict], columns: tuple[str, ...] = RESULT_COLUMNS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_daily_report(repository: TrendCheckRepository, trade_date: str, output_dir: Path, version: Optional[str] = None) -> Optional[Path]:
    report = build_daily_report(repository, trade_date, version)
    if report is None:
        return None
    directory = output_dir / "daily"
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / f"{trade_date}.md"
    markdown_path.write_text(render_daily_markdown(report, trade_date), encoding="utf-8")
    write_csv(directory / f"{trade_date}.csv", report["rows"])
    return markdown_path


# ---------------------------------------------------------------- 週次・月次

def week_bounds(reference: date) -> tuple[date, date]:
    start = reference - timedelta(days=reference.weekday())
    return start, start + timedelta(days=4)


def month_bounds(month_text: str) -> tuple[date, date]:
    start = date.fromisoformat(f"{month_text}-01")
    following = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, following - timedelta(days=1)


def _stat_table(title: str, entries: list[dict]) -> list[str]:
    lines = [f"### {title}", "| 区分 | 選定 | 候補全体 |", "|---|---|---|"]
    lines += [f"| {item['key']} | {_stat_text(item['selected'])} | {_stat_text(item['candidate'])} |" for item in entries]
    return lines + [""]


def build_period_report(
    repository: TrendCheckRepository, period_type: str, start: date, end: date, label: str
) -> Optional[dict]:
    versions = repository.versions_with_rows(start.isoformat(), end.isoformat())
    if not versions:
        return None
    version = versions[-1]
    rows = repository.load_rows(version, start.isoformat(), end.isoformat())
    summaries = repository.load_summaries(version, start.isoformat(), end.isoformat())
    comparisons = compare_with_other_versions(repository, version, start.isoformat(), end.isoformat())
    agg = aggregate(rows, summaries, with_progression=(period_type == "monthly"))
    weekly = []
    if period_type == "monthly":
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            week_start, _ = week_bounds(date.fromisoformat(row["trade_date"]))
            grouped[week_start.isoformat()].append(row)
        for week_start, week_rows in sorted(grouped.items()):
            weekly.append({
                "week_start": week_start,
                "selected": _stat([row for row in week_rows if row["in_selected"]]), "candidate": _stat(week_rows),
            })
    return {
        "period_type": period_type, "label": label, "start": start.isoformat(), "end": end.isoformat(),
        "version": version, "version_info": repository.get_version(version), "aggregate": agg,
        "comparisons": comparisons, "weekly": weekly, "rows": rows,
    }


def render_period_markdown(report: dict) -> str:
    agg, kind = report["aggregate"], "週次" if report["period_type"] == "weekly" else "月次"
    sources = [UNIVERSE_DESCRIPTIONS.get(source, source) for source in agg["universe_sources"]]
    lines = [
        f"# トレンド答え合わせ({kind}) {report['label']}",
        "",
        f"- 期間: {report['start']} ～ {report['end']} (日次結果{agg['days']}日分を積み上げ。再計算はしていません)",
        f"- 集計に使った版: **{report['version']}** (この期間で結果がある最新版。同じ版の行だけで集計)",
        f"- 候補全体の出所: {' / '.join(sources)}",
        "",
        *_definition_block(report["version_info"]),
        "## 結論",
        *[f"- {line}" for line in conclusion_lines(agg)],
        "",
    ]
    if kind == "月次":
        progression = agg["progression"]
        lines += ["## 月内の推移(選定のトレンド割合)"]
        if progression["first_half"] is not None:
            lines.append(
                f"- 前半平均 {pct(progression['first_half'])} → 後半平均 {pct(progression['second_half'])} "
                f"=> **{progression['direction']}**(±{TREND_SLOPE_HALF_PT:.0f}pt未満は横ばい、傾き {progression['slope_pt_per_day']:+.2f}pt/日。参考)"
            )
        else:
            lines.append(f"- {progression['direction']}")
        lines += ["", "| 週(月曜) | 選定 | 候補全体 |", "|---|---|---|"]
        lines += [f"| {w['week_start']} | {_stat_text(w['selected'])} | {_stat_text(w['candidate'])} |" for w in report["weekly"]]
        lines.append("")

    stdev_text = "-" if agg["stdev_selected_rate"] is None else f"{agg['stdev_selected_rate'] * 100:.1f}pt"
    gap = (
        "-" if agg["mean_selected_rate"] is None or agg["mean_candidate_rate"] is None
        else f"{(agg['mean_selected_rate'] - agg['mean_candidate_rate']) * 100:+.1f}pt"
    )
    pooled_gap = (
        "-" if agg["pooled_selected"]["rate"] is None or agg["pooled_candidate"]["rate"] is None
        else f"{(agg['pooled_selected']['rate'] - agg['pooled_candidate']['rate']) * 100:+.1f}pt"
    )
    lines += [
        "## トレンド割合",
        f"- 日次割合の平均(選定): {pct(agg['mean_selected_rate'])} / 日ごとのばらつき(標準偏差): {stdev_text}",
        f"- 日次割合の平均(候補全体): {pct(agg['mean_candidate_rate'])} / 差(選定-候補全体): {gap}",
        f"- 件数を合算した割合: 選定 {_stat_text(agg['pooled_selected'])} / 候補全体 {_stat_text(agg['pooled_candidate'])} "
        f"/ 選定以外 {_stat_text(agg['pooled_not_selected'])} / 差 {pooled_gap}",
        "",
        "| 日付 | レジーム | 選定 | 候補全体 |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {day['date']} | {day['regime'] or UNKNOWN} | {day['selected_trend']}/{day['selected_judged']} ({pct(day['selected_rate'])}) "
        f"| {pct(day['candidate_rate'])} |" for day in agg["daily"]
    ]
    lines += ["", *_stat_table("レジーム別", agg["by_regime"])]
    for feature in agg["by_feature"].values():
        lines += _stat_table(f"特徴別: {feature['title']}", feature["buckets"])

    bought = agg["bought"]
    lines += [
        "## 買った銘柄",
        f"- 注文記録を確認できた選定行: {bought['known_selected_rows']}件 / 確認できず(不明): {bought['unknown_selected_rows']}件",
        f"- 買った銘柄のうちトレンドだった割合: {_stat_text(bought['stat'])} (買った計{bought['bought_count']}件)",
        f"- 損益(手数料前・決済済みのみ): 合計 {num(bought['pnl_total'])}円 / 平均 {num(bought['pnl_average'])}円 / "
        f"プラス {bought['win_count']}/{bought['pnl_count']}件",
        f"- トレンドだった銘柄の損益合計 {num(bought['trend_pnl_total'])}円({bought['trend_pnl_count']}件) / "
        f"それ以外 {num(bought['other_pnl_total'])}円({bought['other_pnl_count']}件)",
        "",
        "## 判定不能",
        f"- 選定 {agg['undecidable_selected']}件 / 候補全体 {agg['undecidable_candidate']}件(割合の分母に含めていません)",
    ]
    if agg["undecidable_dates"]:
        lines.append(f"- 選定に判定不能がある日: {', '.join(agg['undecidable_dates'])}")
    lines += ["", "## 旧版との差", *_render_comparison(report["version"], report["comparisons"]), ""]
    lines += [
        "## 限界",
        f"- 件数が{MIN_CATEGORY_N}件未満の区分は「※参考値」、総数が{MIN_TOTAL_N}件未満の結論は参考として扱ってください。",
        "- 売買代金倍率はフィルタ診断が保存された日のみ、レジーム・損益は日次レポートが残る日のみ入ります(無い日は「不明」)。",
        "- 判定基準は仮置きです。基準を変えると割合が動くため、旧版との差の表で基準変更の影響を切り分けてください。",
        "- サンプルが小さく、相場環境の影響も受けるため、因果や戦略の優劣は断定できません。",
    ]
    return "\n".join(lines) + "\n"


def period_csv_rows(report: dict) -> list[dict]:
    agg = report["aggregate"]
    records = []

    def add(section: str, key: str, selected: dict, candidate: dict) -> None:
        records.append({
            "section": section, "key": key,
            "selected_n": selected["n"], "selected_trend": selected["trend"], "selected_rate": selected["rate"],
            "candidate_n": candidate["n"], "candidate_trend": candidate["trend"], "candidate_rate": candidate["rate"],
            "reference_only": int(selected["n"] < MIN_CATEGORY_N),
        })

    add("overall", report["version"], agg["pooled_selected"], agg["pooled_candidate"])
    for item in agg["by_regime"]:
        add("regime", item["key"], item["selected"], item["candidate"])
    for name, feature in agg["by_feature"].items():
        for item in feature["buckets"]:
            add(f"feature:{name}", item["key"], item["selected"], item["candidate"])
    for week in report["weekly"]:
        add("week", week["week_start"], week["selected"], week["candidate"])
    for day in agg["daily"]:
        records.append({
            "section": "day", "key": day["date"], "selected_n": day["selected_judged"],
            "selected_trend": day["selected_trend"], "selected_rate": day["selected_rate"],
            "candidate_rate": day["candidate_rate"],
        })
    return records


PERIOD_CSV_COLUMNS = (
    "section", "key", "selected_n", "selected_trend", "selected_rate",
    "candidate_n", "candidate_trend", "candidate_rate", "reference_only",
)


def write_period_report(
    repository: TrendCheckRepository, period_type: str, start: date, end: date, label: str, output_dir: Path
) -> Optional[Path]:
    report = build_period_report(repository, period_type, start, end, label)
    if report is None:
        return None
    directory = output_dir / period_type
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / f"{label}.md"
    markdown_path.write_text(render_period_markdown(report), encoding="utf-8")
    write_csv(directory / f"{label}.csv", period_csv_rows(report), PERIOD_CSV_COLUMNS)
    return markdown_path


def write_recompute_report(
    repository: TrendCheckRepository, version: str, summaries: list[dict], output_dir: Path
) -> Path:
    """全期間再計算の結果(件数・判定不能の内訳・最新日の一覧)をまとめる。"""
    info = repository.get_version(version)
    lines = [
        f"# トレンド答え合わせ 全期間再計算 {version}",
        "",
        f"- 実行: {datetime.now().isoformat(timespec='seconds')} / 対象日数: {len(summaries)}日",
        "- 旧版の結果は消さず残しています。同じ日・同じ版は上書きです。",
        "",
        *_definition_block(info),
        "## 日別の件数",
        "| 日付 | 選定(判定/総数) | トレンド | 選定割合 | 候補全体(判定/総数) | 候補割合 | 判定不能(選定/候補) | 候補の出所 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in summaries:
        lines.append(
            f"| {item['trade_date']} | {item['selected_judged']}/{item['selected_count']} | {item['selected_trend']} | "
            f"{pct(item['selected_rate'])} | {item['candidate_judged']}/{item['candidate_count']} | "
            f"{pct(item['candidate_rate'])} | {item['selected_undecidable']}/{item['candidate_undecidable']} | "
            f"{item['universe_source']} |"
        )
    undecidable_days = [item for item in summaries if item["selected_undecidable"] or item["candidate_undecidable"]]
    lines += [
        "",
        "## 判定不能(日足キャッシュの欠け)",
        f"- 選定: 合計{sum(item['selected_undecidable'] for item in summaries)}件 / "
        f"候補全体: 合計{sum(item['candidate_undecidable'] for item in summaries)}件(割合の分母から除外)",
        f"- 該当日: {', '.join(item['trade_date'] for item in undecidable_days) or 'なし'}",
        "",
    ]
    if any(item["universe_source"] == UNIVERSE_DAILY_CACHE for item in summaries):
        lines.append("- 注意: 一部の日は候補全体を日足キャッシュ全銘柄で代用しています。")
    directory = output_dir / "recompute"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"recompute_{version}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
