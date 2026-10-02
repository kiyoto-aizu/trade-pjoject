"""MARKET_REGIME_DANGER_SKIPを日経感応度・流動性の区分別に集計する読み取り専用の診断スクリプト。

実行方法:
    python scripts/analysis/analyze_regime_skip_by_sensitivity.py

DB・日足キャッシュは読み取り専用で開き、レポートは診断出力ディレクトリにのみ書き出す。
感応度・流動性はイベント日より前のデータだけで計算する（先読みなし）。
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "yahoo_daily"
OUTPUT_DIR = PROJECT_ROOT / "data" / "backtest_v2_scratch" / "diagnostics"
PAPER_DB = PROJECT_ROOT / "data" / "state" / "filter_decision_events.sqlite3"
BACKTEST_DB = PROJECT_ROOT / "data" / "backtest" / "state" / "filter_decision_events.sqlite3"
INDEX_SYMBOL = "^N225"
EVENT_TYPE = "MARKET_REGIME_DANGER_SKIP"
WINDOW = 60
MIN_OBSERVATIONS = 40
TURNOVER_WINDOW = 20
EXCLUDED_DAY = "2026-10-02"
TERCILE_LABELS = ("低", "中", "高")
OUTCOME_AVOIDED = "損失回避の可能性"
OUTCOME_MISSED = "利益取り逃しの可能性"


def daily_returns(closes: Mapping[str, float]) -> dict[str, float]:
    """日付昇順の終値から、前営業日比の日次リターンを返す。"""
    dates = sorted(closes)
    returns: dict[str, float] = {}
    for previous, current in zip(dates, dates[1:]):
        base = closes[previous]
        if base and base > 0:
            returns[current] = closes[current] / base - 1.0
    return returns


def correlation_and_beta(
    stock_returns: Mapping[str, float],
    index_returns: Mapping[str, float],
    before_date: str | None = None,
    window: int | None = WINDOW,
    min_observations: int = MIN_OBSERVATIONS,
) -> dict[str, float] | None:
    """before_date未満の共通日付のうち直近window本で相関とベータを計算する。不足・分散ゼロはNone。"""
    common = sorted(
        day for day in stock_returns
        if day in index_returns and (before_date is None or day < before_date)
    )
    if window is not None:
        common = common[-window:]
    if len(common) < min_observations:
        return None
    x = [index_returns[day] for day in common]
    y = [stock_returns[day] for day in common]
    mean_x, mean_y = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((value - mean_x) ** 2 for value in x)
    syy = sum((value - mean_y) ** 2 for value in y)
    sxy = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    if sxx <= 0 or syy <= 0:
        return None
    return {"correlation": sxy / math.sqrt(sxx * syy), "beta": sxy / sxx, "observations": float(len(common))}


def average_turnover(
    closes: Mapping[str, float],
    volumes: Mapping[str, float] | None,
    before_date: str,
    window: int = TURNOVER_WINDOW,
) -> float | None:
    """before_date未満の直近window日の平均売買代金（終値×出来高）。出来高なし・不足はNone。"""
    if not volumes:
        return None
    days = sorted(day for day in closes if day < before_date and day in volumes)[-window:]
    if len(days) < window:
        return None
    return statistics.fmean(closes[day] * volumes[day] for day in days)


def assign_terciles(values: Mapping[Any, float]) -> dict[Any, str]:
    """値の順位で3等分（低/中/高）を割り当てる。同値は安定ソート順で振り分ける。"""
    ordered = sorted(values, key=lambda key: (values[key], str(key)))
    count = len(ordered)
    return {key: TERCILE_LABELS[min(2, index * 3 // count)] for index, key in enumerate(ordered)}


def dedupe_symbol_day(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """銘柄×日付で重複を除く。確定済みを優先し、次に最後に記録されたものを残す。"""
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for event in sorted(events, key=lambda e: (str(e["occurred_at"]), int(e.get("id") or 0))):
        key = (str(event["symbol"]), str(event["occurred_at"])[:10])
        current = chosen.get(key)
        if current is None or not (current.get("status") == "finalized" and event.get("status") != "finalized"):
            chosen[key] = dict(event)
    return list(chosen.values())


def summarize_group(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """損失回避・取り逃し・値動きなし・未確定の件数、取り逃し割合、概算損益の合計と中央値。"""
    avoided = sum(e.get("outcome") == OUTCOME_AVOIDED for e in events)
    missed = sum(e.get("outcome") == OUTCOME_MISSED for e in events)
    flat = sum(e.get("status") == "finalized" and e.get("outcome") not in (OUTCOME_AVOIDED, OUTCOME_MISSED) for e in events)
    pending = sum(e.get("status") != "finalized" for e in events)
    pnls = [float(e["hypothetical_pnl_before_cost"]) for e in events
            if e.get("status") == "finalized" and e.get("hypothetical_pnl_before_cost") is not None]
    decided = avoided + missed
    return {
        "n": len(events),
        "loss_avoided": avoided,
        "missed": missed,
        "flat": flat,
        "pending": pending,
        "missed_ratio": missed / decided if decided else None,
        "pnl_sum": sum(pnls) if pnls else None,
        "pnl_median": statistics.median(pnls) if pnls else None,
    }


def group_table(events: Sequence[Mapping[str, Any]], key_fields: Sequence[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        groups[tuple(str(event[field]) for field in key_fields)].append(event)
    rows = []
    for key in sorted(groups):
        rows.append({**dict(zip(key_fields, key)), **summarize_group(groups[key])})
    return rows


def load_cache(symbol: str) -> dict[str, dict[str, float]] | None:
    path = CACHE_DIR / f"{symbol}.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def load_events(database: Path, mode: str) -> list[dict[str, Any]]:
    if not database.exists():
        return []
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT id, symbol, execution_mode, occurred_at, status, outcome,
                      hypothetical_pnl_before_cost, price_change_percent, reference_price, quantity
               FROM filter_decision_events
               WHERE event_type = ? AND execution_mode = ? ORDER BY occurred_at, id""",
            (EVENT_TYPE, mode),
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "source": mode} for row in rows]


def enrich(events: list[dict[str, Any]], index_returns: Mapping[str, float]) -> None:
    """各イベントに直近60営業日版・全期間版の感応度と20日売買代金を付与する（イベント日より前のみ）。"""
    cache: dict[str, dict | None] = {}
    for event in events:
        symbol, day = event["symbol"], event["occurred_at"][:10]
        if symbol not in cache:
            cache[symbol] = load_cache(symbol)
        raw = cache[symbol]
        event["corr"] = event["beta"] = event["corr_obs"] = event["corr_all"] = event["beta_all"] = None
        event["turnover"] = None
        if not raw:
            event["unavailable_reason"] = "キャッシュなし"
            continue
        closes = {d: float(v["close"]) for d, v in raw.items() if v.get("close") is not None}
        volumes = {d: float(v["volume"]) for d, v in raw.items() if v.get("volume") is not None}
        returns = daily_returns(closes)
        recent = correlation_and_beta(returns, index_returns, day)
        if recent:
            event["corr"], event["beta"], event["corr_obs"] = recent["correlation"], recent["beta"], int(recent["observations"])
        full = correlation_and_beta(returns, index_returns, None, window=None)
        if full:
            event["corr_all"], event["beta_all"] = full["correlation"], full["beta"]
        event["turnover"] = average_turnover(closes, volumes, day)
        event["unavailable_reason"] = "" if recent else "直近60営業日で40本未満または分散ゼロ"


def assign_buckets(events: list[dict[str, Any]]) -> None:
    for field, bucket_field in (("corr", "corr_bucket"), ("corr_all", "corr_all_bucket"), ("turnover", "turnover_bucket")):
        valid = {i: e[field] for i, e in enumerate(events) if e.get(field) is not None}
        labels = assign_terciles(valid) if valid else {}
        for i, event in enumerate(events):
            event[bucket_field] = labels.get(i, "算出不能")
    for event in events:
        event["combo_bucket"] = f"相関{event['corr_bucket']}×売買代金{event['turnover_bucket']}"


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def table_markdown(rows: list[dict[str, Any]], key_fields: Sequence[str]) -> list[str]:
    header = [*key_fields, "n", "損失回避", "取り逃し", "値動きなし", "未確定", "取り逃し割合", "概算損益合計", "概算損益中央値"]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        ratio = f"{row['missed_ratio']:.1%}" if row["missed_ratio"] is not None else "-"
        cells = [*(row[f] for f in key_fields), row["n"], row["loss_avoided"], row["missed"], row["flat"],
                 row["pending"], ratio, fmt(row["pnl_sum"], 0), fmt(row["pnl_median"], 0)]
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    return lines


def build_scopes(paper: list[dict], backtest: list[dict]) -> dict[str, list[dict]]:
    paper_unique = dedupe_symbol_day(paper)
    backtest_unique = dedupe_symbol_day(backtest)
    combined = dedupe_symbol_day([*backtest, *paper])  # 同キーは確定済み優先、同条件なら後の記録
    return {"paper": paper_unique, "backtest": backtest_unique, "paper+backtest": combined}


def main() -> None:
    index_raw = load_cache(INDEX_SYMBOL)
    if not index_raw:
        raise SystemExit(f"{INDEX_SYMBOL}のキャッシュがありません")
    index_returns = daily_returns({d: float(v["close"]) for d, v in index_raw.items()})

    paper_raw = load_events(PAPER_DB, "paper")
    backtest_raw = load_events(BACKTEST_DB, "backtest")
    scopes = build_scopes(paper_raw, backtest_raw)
    shared = {(e["symbol"], e["occurred_at"][:10]) for e in scopes["paper"]} & \
             {(e["symbol"], e["occurred_at"][:10]) for e in scopes["backtest"]}

    all_rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {"scopes": {}}
    md = ["# MARKET_REGIME_DANGER_SKIP 日経感応度・流動性別 診断レポート", "",
          "読み取り専用の記述的分析。ロジック変更の提案は含まない。", ""]
    has_volume = any(
        "volume" in row for path in CACHE_DIR.glob("*.json") if not path.stem.startswith("^")
        for row in (load_cache(path.stem) or {}).values()
    )
    md += ["## 前提", f"- 入力: paper={PAPER_DB.relative_to(PROJECT_ROOT)} / backtest={BACKTEST_DB.relative_to(PROJECT_ROOT)}",
           f"- 元イベント: paper {len(paper_raw)}件, backtest {len(backtest_raw)}件 / 銘柄×日付ユニーク: paper {len(scopes['paper'])}件, "
           f"backtest {len(scopes['backtest'])}件, 合算 {len(scopes['paper+backtest'])}件 (paperとbacktestで銘柄×日付が一致: {len(shared)}件)",
           f"- 感応度: 直近{WINDOW}営業日(最低{MIN_OBSERVATIONS}本)、イベント日より前のみ。参考として全期間版も併記",
           f"- 出来高: {'あり' if has_volume else 'キャッシュに無いため売買代金の区分は省略'}",
           "- 未確定(observing)は損益・割合の対象外で「未確定」列に件数のみ表示", ""]

    for scope_name, events in scopes.items():
        for variant, excluded in (("10/2除外", True), ("10/2含む", False)):
            subset = [dict(e) for e in events if not (excluded and e["occurred_at"][:10] == EXCLUDED_DAY)]
            enrich(subset, index_returns)
            assign_buckets(subset)
            unavailable = [e for e in subset if e["corr"] is None]
            md += [f"## {scope_name} / {variant}", f"- 集計対象 {len(subset)}件 (相関算出可 {len(subset) - len(unavailable)}件 / 算出不能 {len(unavailable)}件)", ""]
            tables = {"corr_bucket": ["corr_bucket"]}
            if has_volume:
                tables["turnover_bucket"] = ["turnover_bucket"]
                tables["combo_bucket"] = ["combo_bucket"]
            tables["corr_all_bucket(参考:全期間・先読みあり)"] = ["corr_all_bucket"]
            report["scopes"][f"{scope_name}/{variant}"] = {"n": len(subset), "unavailable": len(unavailable), "tables": {}}
            for title, fields in tables.items():
                rows = group_table(subset, fields)
                md += [f"### {title}", *table_markdown(rows, fields), ""]
                report["scopes"][f"{scope_name}/{variant}"]["tables"][title] = rows
            corrs = [e["corr"] for e in subset if e["corr"] is not None]
            if corrs:
                md += [f"相関の分布: 最小 {min(corrs):.2f} / 中央値 {statistics.median(corrs):.2f} / 最大 {max(corrs):.2f}", ""]
            for e in subset:
                all_rows.append({"scope": scope_name, "variant": variant, **{k: e.get(k) for k in (
                    "symbol", "occurred_at", "execution_mode", "status", "outcome", "hypothetical_pnl_before_cost",
                    "price_change_percent", "corr", "beta", "corr_obs", "corr_bucket", "corr_all", "beta_all",
                    "corr_all_bucket", "turnover", "turnover_bucket", "unavailable_reason")}})

    md += ["## 限界",
           "- サンプルが非常に小さい。paperは10/2の2件のみ（観測中で結果未確定）、backtestもユニーク約20件で、3分位ごとの件数は数件程度。割合・中央値は偶然の影響が大きい。",
           "- 相関・ベータは直近60営業日(最低40本)の日次リターンから推定しており、推定誤差が大きい。3分位の境界付近の銘柄は区分が入れ替わりやすい。",
           "- 日足キャッシュは2026-09-25までで、それ以降のイベントは最大数営業日古いデータでの推定になる。",
           "- 概算損益は実取引損益ではなく、見送り時点の参考価格と観測終了時価格の差×数量（コスト前）。",
           "- backtestはpaperとは別の再現環境で生成されたイベントであり、同一の銘柄×日付が重複し得る。",
           "- 日経の日次リターンは当日ギャップを含む終値ベースで、レジーム判定に使う指標（日中変動率等）とは異なる。"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "regime_skip_sensitivity_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "regime_skip_sensitivity_diagnostics.json").write_text(
        json.dumps({**report, "has_volume": has_volume}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (OUTPUT_DIR / "regime_skip_sensitivity_events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(all_rows[0]) if all_rows else ["scope"])
        writer.writeheader()
        writer.writerows(all_rows)
    print("\n".join(md))


if __name__ == "__main__":
    main()
