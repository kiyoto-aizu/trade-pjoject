"""見送りイベントの損失回避/取り逃しを「5営業日観測」基準と「当日基準」で比較する読み取り専用の診断スクリプト。

実行方法:
    python scripts/analysis/analyze_skip_same_day_basis.py

当日基準:
    A: 日足の始値→終値の変化率 × 参照価格 × 数量（見送り時刻不問の近似）
    B: 見送り時の参照価格→15:20時点（15:10〜15:20の最後の分足、無ければ当日15:10以降の観測値）の損益
    C: ATR損切り・利確の再生は、レジーム系イベントにATR入力が無いため未実装
DB・キャッシュ・分足は読み取り専用で開く。
"""
from __future__ import annotations

import csv
import json
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis.analyze_regime_skip_by_sensitivity import (  # noqa: E402
    BACKTEST_DB, EXCLUDED_DAY, INDEX_SYMBOL, OUTPUT_DIR, OUTCOME_AVOIDED, OUTCOME_MISSED, PAPER_DB,
    assign_buckets, daily_returns, dedupe_symbol_day, enrich, fmt, group_table, load_cache, table_markdown,
)

MINUTE_DIR = PROJECT_ROOT / "data" / "minute_bars_parquet"
EVENT_TYPES = ("MARKET_REGIME_DANGER_SKIP", "ATR_DANGER_SKIP", "MARKET_REGIME_CAUTION_RSI_FILTER")
MAIN_EVENT_TYPE = EVENT_TYPES[0]
CUTOFF_TIME = "15:20:00"
EARLIEST_END_TIME = "15:10:00"
OUTCOME_FLAT = "値動きなし"
OUTCOME_PENDING = "未確定"
OUTCOME_NA = "算出不能"


def classify_outcome(pnl: float | None) -> str:
    """買い見送りの概算損益から分類する（FilterDecisionRepository._summarizeと同じ規則）。"""
    if pnl is None:
        return OUTCOME_NA
    if pnl < 0:
        return OUTCOME_AVOIDED
    if pnl > 0:
        return OUTCOME_MISSED
    return OUTCOME_FLAT


def open_to_close_pnl(reference_price: float, quantity: int, open_price: float | None, close_price: float | None) -> float | None:
    """始値→終値の変化率 × 参照価格 × 数量。"""
    if open_price is None or close_price is None or open_price <= 0:
        return None
    return (close_price / open_price - 1.0) * reference_price * quantity


def price_at_or_before(
    bars: Sequence[tuple[str, float]], cutoff: str, earliest: str | None = None
) -> float | None:
    """cutoff以前（先読みなし）で最後の分足価格。その時刻がearliest未満ならNone。"""
    chosen: tuple[str, float] | None = None
    for bar_time, price in bars:
        if bar_time <= cutoff and (chosen is None or bar_time >= chosen[0]):
            chosen = (bar_time, price)
    if chosen is None or (earliest is not None and chosen[0] < earliest):
        return None
    return chosen[1]


def minute_same_day_pnl(
    reference_price: float, quantity: int, bars: Sequence[tuple[str, float]], occurred_at: str
) -> tuple[float | None, str]:
    """見送り時の参照価格→15:20時点の損益と、算出不能の理由（算出できれば空文字）。"""
    if occurred_at[11:19] == "00:00:00":
        return None, "見送り時刻不明(00:00:00)"
    if not bars:
        return None, "分足なし"
    day = occurred_at[:10]
    earliest = max(f"{day}T{EARLIEST_END_TIME}", occurred_at)
    end_price = price_at_or_before(bars, f"{day}T{CUTOFF_TIME}", earliest)
    if end_price is None:
        return None, "15:10〜15:20の分足なし"
    return (end_price - reference_price) * quantity, ""


def observation_same_day_pnl(
    reference_price: float, quantity: int, last_price: float | None, last_observed_at: str | None, occurred_at: str
) -> float | None:
    """見送り当日の15:10〜15:20に観測されたDB上のlast_priceによる損益（分足が無い場合の代替）。"""
    if last_price is None or not last_observed_at or last_observed_at[:10] != occurred_at[:10]:
        return None
    if not (f"{occurred_at[:10]}T{EARLIEST_END_TIME}" <= last_observed_at <= f"{occurred_at[:10]}T{CUTOFF_TIME}"):
        return None
    return (last_price - reference_price) * quantity


def compare_outcomes(current: str, new: str) -> str:
    """5日基準と当日基準の分類の比較ラベル。"""
    if current in (OUTCOME_PENDING, OUTCOME_NA) or new == OUTCOME_NA:
        return "比較不能"
    if current == new:
        return "変化なし"
    if current == OUTCOME_AVOIDED and new == OUTCOME_MISSED:
        return "損失回避→取り逃し"
    if current == OUTCOME_MISSED and new == OUTCOME_AVOIDED:
        return "取り逃し→損失回避"
    return "その他の変化"


def load_events(database: Path, mode: str) -> list[dict[str, Any]]:
    if not database.exists():
        return []
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in EVENT_TYPES)
        rows = connection.execute(
            f"""SELECT id, event_type, symbol, execution_mode, occurred_at, status, outcome,
                       hypothetical_pnl_before_cost, reference_price, quantity, last_price, last_observed_at
                FROM filter_decision_events WHERE execution_mode = ? AND event_type IN ({marks})
                ORDER BY occurred_at, id""",
            (mode, *EVENT_TYPES),
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "source": mode} for row in rows]


def dedupe_by_type(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event_type in EVENT_TYPES:
        result.extend(dedupe_symbol_day([e for e in events if e["event_type"] == event_type]))
    return result


def _minute_bars(symbol: str, day: str) -> list[tuple[str, float]]:
    from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository

    bars = ParquetMinuteBarRepository(MINUTE_DIR).load_bars(date.fromisoformat(day), symbol)
    return [(bar.time, float(bar.price)) for bar in bars if bar.price is not None]


def evaluate(event: dict[str, Any]) -> None:
    """現行・A・Bの損益と分類をイベントへ付与する。"""
    ref, qty = float(event["reference_price"]), int(event["quantity"])
    day = event["occurred_at"][:10]
    finalized = event["status"] == "finalized"
    event["current_pnl"] = event["hypothetical_pnl_before_cost"] if finalized else None
    event["current_outcome"] = event["outcome"] if finalized and event["outcome"] else OUTCOME_PENDING

    raw = load_cache(event["symbol"])
    row = (raw or {}).get(day)
    if raw is None:
        event["a_reason"] = "日足キャッシュなし"
    elif row is None:
        event["a_reason"] = "当日の日足なし(キャッシュ期間外)"
    else:
        event["a_reason"] = ""
    event["a_pnl"] = open_to_close_pnl(ref, qty, row.get("open") if row else None, row.get("close") if row else None)

    bars = _minute_bars(event["symbol"], day)
    pnl, reason = minute_same_day_pnl(ref, qty, bars, event["occurred_at"])
    event["b_source"] = "分足" if pnl is not None else ""
    if pnl is None:
        fallback = observation_same_day_pnl(
            ref, qty, event.get("last_price"), event.get("last_observed_at"), event["occurred_at"]
        )
        if fallback is not None:
            pnl, reason, event["b_source"] = fallback, "", "DB観測値(15:10〜15:20)"
    event["b_pnl"], event["b_reason"] = pnl, reason
    for key in ("a", "b"):
        event[f"{key}_outcome"] = classify_outcome(event[f"{key}_pnl"])
        event[f"{key}_change"] = compare_outcomes(event["current_outcome"], event[f"{key}_outcome"])


def basis_summary(events: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    """基準ごとの損失回避・取り逃し件数と概算損益。算出不能は別に数える。"""
    usable = [e for e in events if e[f"{basis}_outcome"] != OUTCOME_NA and (basis != "current" or e["current_outcome"] != OUTCOME_PENDING)]
    pnls = [float(e[f"{basis}_pnl"]) for e in usable]
    avoided = sum(e[f"{basis}_outcome"] == OUTCOME_AVOIDED for e in usable)
    missed = sum(e[f"{basis}_outcome"] == OUTCOME_MISSED for e in usable)
    return {
        "n_total": len(events), "n_usable": len(usable), "n_unavailable": len(events) - len(usable),
        "loss_avoided": avoided, "missed": missed, "flat": len(usable) - avoided - missed,
        "missed_ratio": missed / (avoided + missed) if avoided + missed else None,
        "pnl_sum": sum(pnls) if pnls else None, "pnl_median": statistics.median(pnls) if pnls else None,
    }


def as_basis_events(events: Sequence[Mapping[str, Any]], basis: str) -> list[dict[str, Any]]:
    """summarize_group/group_tableが読める形式へ当日基準の結果を写す（算出不能は除外）。"""
    return [
        {**e, "status": "finalized", "outcome": e[f"{basis}_outcome"], "hypothetical_pnl_before_cost": e[f"{basis}_pnl"]}
        for e in events if e[f"{basis}_outcome"] != OUTCOME_NA
    ]


def main() -> None:
    index_raw = load_cache(INDEX_SYMBOL)
    if not index_raw:
        raise SystemExit(f"{INDEX_SYMBOL}のキャッシュがありません")
    index_returns = daily_returns({d: float(v["close"]) for d, v in index_raw.items()})

    paper = dedupe_by_type(load_events(PAPER_DB, "paper"))
    backtest = dedupe_by_type(load_events(BACKTEST_DB, "backtest"))
    scopes = {"paper": paper, "backtest": backtest, "paper+backtest": [*backtest, *paper]}

    md = ["# 見送りイベントの5営業日基準 vs 当日基準 診断レポート", "",
          "読み取り専用の記述的分析。ロジック変更の提案は含まない。", "",
          "## 前提",
          f"- 対象種別: {', '.join(EVENT_TYPES)}（銘柄×日付・種別ごとに重複除去。バックアップDB・scratch配下は除外）",
          "- 現行: DBのoutcome/概算損益（確定済みのみ。観測中は「未確定」で比較不能）",
          "- A: 日足の始値→終値の変化率 × 参照価格 × 数量（見送り時刻不問。キャッシュは9/25まで）",
          f"- B: 参照価格→{CUTOFF_TIME[:5]}時点の損益。{EARLIEST_END_TIME[:5]}〜{CUTOFF_TIME[:5]}の最後の分足（先読みなし）。"
          "分足が無い場合のみ、同日15:10〜15:20に観測されたDBのlast_priceで代替し、列b_sourceで区別",
          "- C（ATR損切り・利確の再生）: レジーム系イベントにATR・ATR水準が記録されておらず、当日エントリー条件の再現に日足ATRの再構築が必要なため未実装",
          ""]
    report: dict[str, Any] = {"scopes": {}}
    csv_rows: list[dict[str, Any]] = []
    csv_fields = ("scope", "variant", "event_type", "source", "symbol", "occurred_at", "status", "reference_price", "quantity",
                  "current_outcome", "current_pnl", "a_outcome", "a_pnl", "a_change", "a_reason",
                  "b_outcome", "b_pnl", "b_change", "b_source", "b_reason",
                  "corr", "beta", "corr_bucket", "unavailable_reason")

    evaluated: dict[int, dict[str, Any]] = {}
    for scope_name, events in scopes.items():
        for variant, exclude in (("10/2除外", True), ("10/2含む", False)):
            subset = [dict(e) for e in events if not (exclude and e["occurred_at"][:10] == EXCLUDED_DAY)]
            for event in subset:
                key = (event["source"], event["event_type"], event["symbol"], event["occurred_at"][:10])
                if key not in evaluated:
                    evaluate(event)
                    evaluated[key] = event
                else:
                    event.update({k: v for k, v in evaluated[key].items() if k not in event})
            enrich(subset, index_returns)
            assign_buckets(subset)
            label = f"{scope_name} / {variant}"
            md += [f"## {label}", ""]
            report["scopes"][label] = {}
            for event_type in EVENT_TYPES:
                typed = [e for e in subset if e["event_type"] == event_type]
                md += [f"### {event_type} (ユニーク {len(typed)}件)", "",
                       "| 基準 | 対象 | 算出可 | 算出不能 | 損失回避 | 取り逃し | 値動きなし | 取り逃し割合 | 概算損益合計 | 概算損益中央値 |",
                       "|---|---|---|---|---|---|---|---|---|---|"]
                rows = {}
                for basis, name in (("current", "現行(5営業日)"), ("a", "A 日足始値→終値"), ("b", "B 参照価格→15:20")):
                    s = basis_summary(typed, basis)
                    rows[basis] = s
                    ratio = f"{s['missed_ratio']:.1%}" if s["missed_ratio"] is not None else "-"
                    md.append(f"| {name} | {s['n_total']} | {s['n_usable']} | {s['n_unavailable']} | {s['loss_avoided']} | {s['missed']} | "
                              f"{s['flat']} | {ratio} | {fmt(s['pnl_sum'], 0)} | {fmt(s['pnl_median'], 0)} |")
                md.append("")
                for basis, name in (("a", "A"), ("b", "B")):
                    reasons = Counter(e[f"{basis}_reason"] for e in typed if e[f"{basis}_outcome"] == OUTCOME_NA)
                    if reasons:
                        md.append(f"- {name}の算出不能理由: " + ", ".join(f"{r or '不明'} {c}件" for r, c in reasons.most_common()))
                changes = {basis: Counter(e[f"{basis}_change"] for e in typed) for basis in ("a", "b")}
                for basis, name in (("a", "A"), ("b", "B")):
                    md.append(f"- 現行→{name}の分類変化: " + ", ".join(f"{k} {v}件" for k, v in sorted(changes[basis].items())))
                flips = [e for e in typed if e["a_change"] in ("損失回避→取り逃し", "取り逃し→損失回避")
                         or e["b_change"] in ("損失回避→取り逃し", "取り逃し→損失回避")]
                if flips:
                    md += ["", "分類が反転したイベント:", "", "| 銘柄 | 日時 | 現行 | A | B |", "|---|---|---|---|---|"]
                    for e in flips:
                        md.append(f"| {e['symbol']} | {e['occurred_at']} | {e['current_outcome']} ({fmt(e['current_pnl'], 0)}) | "
                                  f"{e['a_outcome']} ({fmt(e['a_pnl'], 0)}) | {e['b_outcome']} ({fmt(e['b_pnl'], 0)}) |")
                md.append("")
                report["scopes"][label][event_type] = {"summary": rows, "changes": {k: dict(v) for k, v in changes.items()}}
                for e in typed:
                    csv_rows.append({"scope": scope_name, "variant": variant, **{k: e.get(k) for k in csv_fields[2:]}})

            main_events = [e for e in subset if e["event_type"] == MAIN_EVENT_TYPE]
            md += [f"#### {MAIN_EVENT_TYPE}: 日経相関3分位別（直近60営業日の相関、前回と同じ区分）", ""]
            for basis, name in (("current", "現行(5営業日)"), ("a", "A 日足始値→終値"), ("b", "B 参照価格→15:20")):
                basis_events = [e for e in main_events if e["current_outcome"] != OUTCOME_PENDING] if basis == "current" else as_basis_events(main_events, basis)
                if basis == "current":
                    basis_events = [{**e, "status": "finalized", "outcome": e["current_outcome"], "hypothetical_pnl_before_cost": e["current_pnl"]} for e in basis_events]
                table = group_table(basis_events, ["corr_bucket"])
                md += [f"**{name}**", "", *table_markdown(table, ["corr_bucket"]), ""]
                report["scopes"][label].setdefault("corr_tables", {})[basis] = table

    md += ["## 限界",
           "- サンプルが非常に小さい。MARKET_REGIME_DANGER_SKIPはpaper 2件（10/2・観測中）、backtest 20件（うち時刻不明が過半）で、3分位ごとは数件。",
           "- 相関の推定は直近60営業日・日次リターンのノイズが大きく、3分位境界付近は入れ替わりやすい。",
           "- A（日足近似）は見送り時刻を考慮せず始値からの変化で、見送り後の実際の値幅とは異なる。日足キャッシュは9/25までで、9/28以降は算出不能。",
           "- B は分足が5分間隔・9/2〜9/25の一部銘柄のみ。backtestイベントの多くはoccurred_atが00:00:00で見送り時刻が不明のため算出不能。",
           "- 10/2のpaperイベントは分足が無く、DB観測値（15:19台のlast_price）での代替になる。10/2は401で板が取れない時間帯があり、含む版と除く版を分けている。",
           "- 概算損益は実取引損益ではなく、コスト・約定可否を含まない。",
           "- C（ATR損切り・利確の再生）は未実装。"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "skip_same_day_basis_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "skip_same_day_basis_diagnostics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (OUTPUT_DIR / "skip_same_day_basis_events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(csv_fields))
        writer.writeheader()
        writer.writerows(csv_rows)
    print("\n".join(md))


if __name__ == "__main__":
    main()
