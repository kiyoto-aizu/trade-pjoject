"""見送りイベントを複数指数相関・売買代金・損益基準別に記述集計する。"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.analysis import analyze_regime_skip_by_sensitivity as sensitivity  # noqa: E402
from scripts.analysis.analyze_skip_same_day_basis import (  # noqa: E402
    _minute_bars,
    classify_outcome,
    minute_same_day_pnl,
    open_to_close_pnl,
)
from src.infrastructure.calendar.japanese_calendar import is_trading_day  # noqa: E402

CACHE_DIR = sensitivity.CACHE_DIR
OUTPUT_DIR = sensitivity.OUTPUT_DIR
PAPER_DB = sensitivity.PAPER_DB
BACKTEST_DB = sensitivity.BACKTEST_DB
EVENT_TYPES = (
    "MARKET_REGIME_DANGER_SKIP",
    "MARKET_REGIME_CAUTION_RSI_FILTER",
    "ATR_DANGER_SKIP",
)
INDEX_SYMBOLS = ("^N225", "2516.T", "1306.T")
INDEX_NAMES = {"^N225": "日経", "2516.T": "グロース250", "1306.T": "TOPIX"}
EXCLUDED_DAY = sensitivity.EXCLUDED_DAY
DAILY_REPLAY_FLAG = "DAILY_REPLAY_NO_INTRADAY"
BOARD_UNAVAILABLE_FLAG = "BOARD_UNAVAILABLE"
CORRELATION_WINDOW = 60
MIN_CORRELATION_OBSERVATIONS = 40
TURNOVER_WINDOW = 20
MIN_TURNOVER_OBSERVATIONS = 15
BUCKET_LABELS = (*sensitivity.TERCILE_LABELS, "算出不能")


def quality_flags(value: object) -> set[str]:
    return {part.split("(", 1)[0].strip() for part in str(value or "").split(",") if part and part != "OK"}


def same_day_exclusion_reason(event: Mapping[str, Any]) -> str:
    flags = quality_flags(event.get("same_day_data_quality"))
    if DAILY_REPLAY_FLAG in flags:
        return DAILY_REPLAY_FLAG
    if BOARD_UNAVAILABLE_FLAG in flags:
        return BOARD_UNAVAILABLE_FLAG
    return ""


def is_exchange_closed_day(event: Mapping[str, Any]) -> bool:
    event_day = date.fromisoformat(str(event["occurred_at"])[:10])
    return not is_trading_day(event_day)


def exclude_holiday_events(events: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    excluded = [dict(event) for event in events if is_exchange_closed_day(event)]
    retained = [dict(event) for event in events if not is_exchange_closed_day(event)]
    return retained, excluded


def same_day_source_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(event["same_day_source"]) for event in events))


def calculate_same_day_result(
    event: Mapping[str, Any], daily_row: Mapping[str, Any] | None,
    bars: Sequence[tuple[str, float]],
) -> dict[str, Any]:
    """same_day close、分足B、日足Aの順に当日損益を計算する純粋関数。"""
    reference_price = float(event["reference_price"])
    quantity = int(event["quantity"])
    same_day_close = event.get("same_day_close_price")
    if same_day_close is not None:
        pnl = (float(same_day_close) - reference_price) * quantity
        source = "same_day_close_price"
        reason = ""
    elif bars:
        pnl, minute_reason = minute_same_day_pnl(reference_price, quantity, bars, str(event["occurred_at"]))
        if pnl is not None:
            source = "分足B"
            reason = ""
        else:
            open_price = daily_row.get("open") if daily_row else None
            close_price = daily_row.get("close") if daily_row else None
            pnl = open_to_close_pnl(reference_price, quantity, open_price, close_price)
            source = "日足A(フォールバック)"
            reason = "" if pnl is not None else f"分足B算出不能({minute_reason}); 当日の日足Aも算出不能"
    else:
        open_price = daily_row.get("open") if daily_row else None
        close_price = daily_row.get("close") if daily_row else None
        pnl = open_to_close_pnl(reference_price, quantity, open_price, close_price)
        source = "日足A"
        reason = "" if pnl is not None else "当日日足なしまたは始値・終値不足"
    return {
        "same_day_pnl": pnl,
        "same_day_outcome": classify_outcome(pnl),
        "same_day_source": source,
        "same_day_unavailable_reason": reason,
    }


def assign_exposure_buckets(events: list[dict[str, Any]], exposure_fields: Sequence[str]) -> None:
    """各エクスポージャーで独立して順位3分位を割り当てる純粋関数。"""
    for field in exposure_fields:
        bucket_field = f"{field}_bucket"
        valid = {index: event[field] for index, event in enumerate(events) if event.get(field) is not None}
        labels = sensitivity.assign_terciles(valid) if valid else {}
        for index, event in enumerate(events):
            event[bucket_field] = labels.get(index, "算出不能")
    for event in events:
        for symbol in INDEX_SYMBOLS:
            corr_bucket = event[f"corr_{symbol}_bucket"]
            turnover_bucket = event["turnover_bucket"]
            event[f"combo_{symbol}_bucket"] = (
                f"{corr_bucket}×{turnover_bucket}"
                if corr_bucket != "算出不能" and turnover_bucket != "算出不能"
                else "算出不能を含む"
            )


def summarize_index_bucket_disagreement(events: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    same_bucket = 0
    different_bucket = 0
    unavailable = 0
    for event in events:
        buckets = [event.get(f"corr_{symbol}_bucket", "算出不能") for symbol in INDEX_SYMBOLS]
        if "算出不能" in buckets:
            unavailable += 1
        elif len(set(buckets)) == 1:
            same_bucket += 1
        else:
            different_bucket += 1
    return {
        "all_three_available": same_bucket + different_bucket,
        "same_tercile_all_three": same_bucket,
        "different_terciles": different_bucket,
        "at_least_one_unavailable": unavailable,
    }


def calculate_event_exposures(
    event_day: str,
    stock_returns: Mapping[str, float],
    stock_closes: Mapping[str, float],
    stock_volumes: Mapping[str, float],
    index_returns: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    """イベント日より前だけのリターン相関・売買代金を返す純粋関数。"""
    exposures: dict[str, Any] = {}
    for symbol, returns in index_returns.items():
        result = sensitivity.correlation_and_beta(
            stock_returns, returns, event_day,
            window=CORRELATION_WINDOW,
            min_observations=MIN_CORRELATION_OBSERVATIONS,
        )
        exposures[f"corr_{symbol}"] = result["correlation"] if result else None
        exposures[f"beta_{symbol}"] = result["beta"] if result else None
        exposures[f"corr_obs_{symbol}"] = int(result["observations"]) if result else None
    exposures["turnover"] = sensitivity.average_turnover(
        stock_closes, stock_volumes, event_day,
        window=TURNOVER_WINDOW,
        min_observations=MIN_TURNOVER_OBSERVATIONS,
    )
    return exposures


def load_events(database: Path, mode: str) -> tuple[list[dict[str, Any]], bool]:
    if not database.exists():
        return [], False
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(filter_decision_events)")}
        rows = connection.execute(
            """SELECT * FROM filter_decision_events
               WHERE event_type IN (?, ?, ?) AND execution_mode = ? ORDER BY occurred_at, id""",
            (*EVENT_TYPES, mode),
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "source": mode} for row in rows], "same_day_data_quality" in columns


def load_paper_events_for_holiday_check(database: Path = PAPER_DB) -> list[dict[str, Any]]:
    if not database.exists():
        return []
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(filter_decision_events)")}
        same_day_observations = "same_day_observation_count" if "same_day_observation_count" in columns else "NULL"
        observations = "observation_count" if "observation_count" in columns else "NULL"
        rows = connection.execute(
            f"""SELECT event_type, symbol, occurred_at,
                       {same_day_observations} AS same_day_observation_count,
                       {observations} AS observation_count
                FROM filter_decision_events WHERE execution_mode = 'paper' ORDER BY occurred_at, id"""
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def dedupe_by_event_type(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for event_type in EVENT_TYPES
        for item in sensitivity.dedupe_symbol_day([event for event in events if event["event_type"] == event_type])
    ]


def summarize_basis(events: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    if basis == "current":
        rows = list(events)
        unavailable = sum(
            event.get("status") == "finalized" and event.get("hypothetical_pnl_before_cost") is None
            for event in rows
        )
    else:
        rows = [
            {
                **event,
                "status": "finalized" if event.get("same_day_pnl") is not None else "observing",
                "outcome": event.get("same_day_outcome"),
                "hypothetical_pnl_before_cost": event.get("same_day_pnl"),
            }
            for event in events
        ]
        unavailable = sum(event.get("same_day_pnl") is None for event in rows)
    summary = sensitivity.summarize_group(rows)
    return {**summary, "unavailable": unavailable}


def _summary_row(label: str, events: Sequence[Mapping[str, Any]], basis: str) -> dict[str, Any]:
    return {"区分": label, **summarize_basis(events, basis)}


def grouped_summary_rows(
    events: Sequence[Mapping[str, Any]], field: str, basis: str,
    labels: Sequence[str],
) -> list[dict[str, Any]]:
    return [
        _summary_row(label, [event for event in events if event.get(field) == label], basis)
        for label in labels
    ]


def format_number(value: Any, digits: int = 0) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def render_table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    headers = ("区分", "サンプル数 n", "損失回避", "取り逃し", "値動きなし", "未確定", "算出不能",
               "取り逃し割合", "概算損益合計", "概算損益中央値")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        ratio = row["missed_ratio"] if row["n"] >= 5 else None
        ratio_text = f"{ratio:.1%}" if ratio is not None else "-"
        cells = (
            row["区分"], row["n"], row["loss_avoided"], row["missed"], row["flat"], row["pending"],
            row["unavailable"], ratio_text, format_number(row["pnl_sum"]), format_number(row["pnl_median"]),
        )
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    return lines


def load_cache_bundle(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict], dict[str, dict[str, float]]]:
    cache: dict[str, dict] = {}
    returns_by_symbol: dict[str, dict[str, float]] = {}
    symbols = {str(event["symbol"]) for event in events} | set(INDEX_SYMBOLS)
    for symbol in symbols:
        raw = sensitivity.load_cache(symbol) or {}
        cache[symbol] = raw
        closes = {day: float(row["close"]) for day, row in raw.items() if row.get("close") is not None}
        returns_by_symbol[symbol] = sensitivity.daily_returns(closes)
    return cache, returns_by_symbol


def enrich_events(
    events: list[dict[str, Any]], cache: Mapping[str, Mapping[str, Mapping[str, Any]]],
    returns_by_symbol: Mapping[str, Mapping[str, float]], index_returns: Mapping[str, Mapping[str, float]],
) -> None:
    for event in events:
        symbol = str(event["symbol"])
        day = str(event["occurred_at"])[:10]
        raw = cache.get(symbol, {})
        closes = {d: float(row["close"]) for d, row in raw.items() if row.get("close") is not None}
        volumes = {d: float(row["volume"]) for d, row in raw.items() if row.get("volume") is not None}
        event.update(calculate_event_exposures(
            day, returns_by_symbol.get(symbol, {}), closes, volumes, index_returns,
        ))
        daily_row = raw.get(day)
        try:
            bars = _minute_bars(symbol, day)
        except (OSError, ValueError, ImportError):
            bars = []
        event.update(calculate_same_day_result(event, daily_row, bars))
        event["same_day_exclusion"] = same_day_exclusion_reason(event)


def cache_inventory() -> dict[str, Any]:
    files = sorted(CACHE_DIR.glob("*.json"))
    symbols = [path.stem for path in files if not path.stem.startswith("^")]
    latest = None
    null_volume_symbols = 0
    for symbol in symbols:
        raw = sensitivity.load_cache(symbol) or {}
        if raw:
            last_day = max(raw)
            latest = max(latest, last_day) if latest else last_day
        if any(row.get("volume") is None for row in raw.values()):
            null_volume_symbols += 1
    return {
        "individual_symbols": len(symbols),
        "latest_day": latest,
        "symbols_with_null_volume_days": null_volume_symbols,
        "index_presence": {symbol: (CACHE_DIR / f"{symbol}.json").exists() for symbol in INDEX_SYMBOLS},
    }


def db_inventory(events_by_mode: Mapping[str, Sequence[Mapping[str, Any]]], quality_columns: Mapping[str, bool]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mode, events in events_by_mode.items():
        same_day_fields = sorted({
            field for event in events for field in event if field.startswith("same_day_")
        })
        by_type: dict[str, Any] = {}
        for event_type in EVENT_TYPES:
            typed = [event for event in events if event["event_type"] == event_type]
            by_type[event_type] = {
                "n": len(typed),
                "same_day_any_field_filled": sum(
                    any(event.get(field) is not None for field in same_day_fields) for event in typed
                ),
                "same_day_field_fill_counts": {
                    field: sum(event.get(field) is not None for event in typed)
                    for field in same_day_fields
                },
                "same_day_close_filled": sum(event.get("same_day_close_price") is not None for event in typed),
                "daily_replay_flagged": sum(DAILY_REPLAY_FLAG in quality_flags(event.get("same_day_data_quality")) for event in typed),
                "board_unavailable_flagged": sum(BOARD_UNAVAILABLE_FLAG in quality_flags(event.get("same_day_data_quality")) for event in typed),
            }
        result[mode] = {"quality_column_available": quality_columns[mode], "event_types": by_type}
    return result


def _table_specs() -> list[tuple[str, str, list[str]]]:
    specs = []
    for symbol in INDEX_SYMBOLS:
        specs.append((f"{INDEX_NAMES[symbol]}相関 ({symbol})", f"corr_{symbol}_bucket", list(BUCKET_LABELS)))
    specs.append(("売買代金", "turnover_bucket", list(BUCKET_LABELS)))
    combo_labels = [f"{corr}×{turnover}" for corr in sensitivity.TERCILE_LABELS for turnover in sensitivity.TERCILE_LABELS]
    combo_labels.append("算出不能を含む")
    for symbol in INDEX_SYMBOLS:
        specs.append((f"{INDEX_NAMES[symbol]}相関×売買代金", f"combo_{symbol}_bucket", combo_labels))
    return specs


def previous_report_comparison(report: Mapping[str, Any]) -> str:
    path = OUTPUT_DIR / "skip_same_day_basis_report.md"
    if not path.exists():
        return "前回レポートが見つからず、数値比較なし。"
    text = path.read_text(encoding="utf-8")
    try:
        section = text.split("## backtest / 10/2除外", 1)[1].split("## backtest / 10/2含む", 1)[0]
        event_section = section.split("### MARKET_REGIME_DANGER_SKIP", 1)[1].split("### ATR_DANGER_SKIP", 1)[0]
        row = next(line for line in event_section.splitlines() if line.startswith("| 現行(5営業日) |"))
        values = [part.strip() for part in row.strip("| ").split("|")]
        prior_n, prior_usable, prior_unavailable, avoided, missed, flat = map(int, values[1:7])
        current = report["analyses"]["backtest/10/2除外/MARKET_REGIME_DANGER_SKIP"]["tables"]["current"]
        current_rows = next(rows for name, rows in current.items() if name.endswith("(^N225)"))
        current_totals = {
            field: sum(row[field] for row in current_rows)
            for field in ("n", "loss_avoided", "missed", "flat", "pending", "unavailable")
        }
        unchanged = (
            current_totals["n"] == prior_n
            and current_totals["loss_avoided"] == avoided
            and current_totals["missed"] == missed
            and current_totals["flat"] == flat
            and current_totals["pending"] + current_totals["unavailable"] == prior_unavailable
        )
        comparison = "5営業日基準の件数・分類は前回と一致します。" if unchanged else "5営業日基準の件数・分類は前回から変化しています。"
        return (
            f"前回レポート(backtest/10/2除外)のMARKET_REGIME_DANGER_SKIPは対象{prior_n}件、"
            f"確定{prior_usable}件・未確定等{prior_unavailable}件、損失回避{avoided}/取り逃し{missed}/"
            f"値動きなし{flat}でした。今回の5営業日基準は対象{current_totals['n']}件、"
            f"損失回避{current_totals['loss_avoided']}/取り逃し{current_totals['missed']}/"
            f"値動きなし{current_totals['flat']}/未確定{current_totals['pending']}件。{comparison}"
            "当日損益は前回のA/B併記方式と優先選択・品質除外の適用範囲が異なるため直接比較しません。"
        )
    except (IndexError, StopIteration, ValueError):
        return "前回レポートから比較対象の数値を読み取れず、数値比較なし。"


def main() -> None:
    raw_by_mode: dict[str, list[dict[str, Any]]] = {}
    quality_columns: dict[str, bool] = {}
    for mode, database in (("paper", PAPER_DB), ("backtest", BACKTEST_DB)):
        raw_by_mode[mode], quality_columns[mode] = load_events(database, mode)

    paper_holiday_events = [
        event for event in load_paper_events_for_holiday_check()
        if is_exchange_closed_day(event)
    ]
    cache_stats = cache_inventory()
    database_stats = db_inventory(raw_by_mode, quality_columns)
    all_events = [event for events in raw_by_mode.values() for event in events]
    cache, returns_by_symbol = load_cache_bundle(all_events)
    index_returns = {symbol: returns_by_symbol.get(symbol, {}) for symbol in INDEX_SYMBOLS}

    report: dict[str, Any] = {
        "cache_inventory": cache_stats,
        "database_inventory": database_stats,
        "paper_holiday_events": paper_holiday_events,
        "method": {
            "correlation_window": CORRELATION_WINDOW,
            "min_correlation_observations": MIN_CORRELATION_OBSERVATIONS,
            "turnover_window": TURNOVER_WINDOW,
            "min_turnover_observations": MIN_TURNOVER_OBSERVATIONS,
            "no_lookahead": True,
            "event_types": list(EVENT_TYPES),
        },
        "analyses": {},
    }
    csv_rows: list[dict[str, Any]] = []
    markdown = [
        "# 見送りイベント: 相関・売買代金区分と当日損益",
        "",
        "読み取り専用の記述的集計。損益は概算であり、p値・有意性の評価やロジック変更提案は含めない。",
        "",
        "## 前提確認",
        f"- 個別日足キャッシュ: {cache_stats['individual_symbols']}銘柄、最新日 {cache_stats['latest_day']}、volume null日を含む銘柄 {cache_stats['symbols_with_null_volume_days']}銘柄。",
        "- 指数キャッシュ: " + ", ".join(f"{symbol}={'あり' if present else 'なし'}" for symbol, present in cache_stats["index_presence"].items()) + ".",
        "- 再利用関数: daily_returns / correlation_and_beta / average_turnover / assign_terciles / dedupe_symbol_day / summarize_group / classify_outcome / open_to_close_pnl / minute_same_day_pnl。",
        "- 入力DBは本番とbacktestの指定DBのみ。backupsとscratch配下のDB・イベントファイルは入力対象外。paperとbacktestは別集計。",
        "- raw DB件数・same_day記録件数は下表。backtestの現行DBにsame_day_*列がない場合は、当日列なしとして明示。",
        "",
        "| DB | 種別 | raw件数 | same_day_*いずれか記録あり | same_day_close_priceあり | DAILY_REPLAY_NO_INTRADAY印 | BOARD_UNAVAILABLE印 | 品質列 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for mode in ("paper", "backtest"):
        for event_type in EVENT_TYPES:
            item = database_stats[mode]["event_types"][event_type]
            markdown.append(
                f"| {mode} | {event_type} | {item['n']} | {item['same_day_any_field_filled']} | {item['same_day_close_filled']} | "
                f"{item['daily_replay_flagged']} | {item['board_unavailable_flagged']} | "
                f"{'あり' if database_stats[mode]['quality_column_available'] else 'なし'} |"
            )
    markdown += [
        "",
        "## 定義",
        f"- 相関: イベント日より前の共通日次リターン直近{CORRELATION_WINDOW}本、最低{MIN_CORRELATION_OBSERVATIONS}本。指数ごとに独立した3分位。算出不能は別枠。",
        f"- 売買代金: イベント日より前の直近{TURNOVER_WINDOW}営業日中、終値×出来高が最低{MIN_TURNOVER_OBSERVATIONS}本ある場合の平均。3分位。算出不能は別枠。",
        "- 同日損益: same_day_close_priceがあれば(価格-参照価格)×数量。分足B(見送り時刻から15:20まで)が算出できなければ日足A(始値→終値)へフォールバック。same_day_sourceで分足B / 日足A / 日足A(フォールバック)を区別。",
        "- 損失回避/取り逃し/値動きなしは損益の符号で判定し、既存classify_outcome・summarize_groupと同じ定義。取り逃し割合は損失回避+取り逃しを分母とする。セルn<5の割合は非表示。",
        "- 当日基準の算出不能イベントは未確定列にも含む。算出不能列はその内訳を別途示す。",
        "- 同日基準のみDAILY_REPLAY_NO_INTRADAYとBOARD_UNAVAILABLE印を除外し、件数を別掲。5営業日基準は記録値を維持。種別別表に加え、合算版は銘柄×日付で種別横断の重複を除く。",
        "- paperの休場日イベントは日本市場カレンダー(土日・祝日・年末年始)で判定して分析から除外。下表に当日観測回数とイベント累積観測回数を併記。",
        "",
        "## Paper休場日イベント (分析から除外)",
        f"該当 {len(paper_holiday_events)}件。same_day_observation_countは当日分、observation_countはイベント行の累積観測数。",
        "| 発生日 | 種別 | 銘柄 | 発生時刻 | 当日観測回数 | 累積観測回数 |",
        "|---|---|---|---|---:|---:|",
    ]
    for event in paper_holiday_events:
        markdown.append(
            f"| {event['occurred_at'][:10]} | {event['event_type']} | {event['symbol']} | {event['occurred_at'][11:]} | "
            f"{event['same_day_observation_count'] if event['same_day_observation_count'] is not None else '-'} | "
            f"{event['observation_count'] if event['observation_count'] is not None else '-'} |"
        )
    markdown.append("")

    markdown += [
        "",
    ]

    for mode in ("paper", "backtest"):
        typed_unique = dedupe_by_event_type(raw_by_mode[mode])
        analyses = [(event_type, [event for event in typed_unique if event["event_type"] == event_type]) for event_type in EVENT_TYPES]
        analyses.append(("合算(銘柄×日付dedupe)", sensitivity.dedupe_symbol_day(typed_unique)))
        for variant, include_excluded_day in (("10/2除外", False), ("10/2含む", True)):
            markdown += [f"## {mode} / {variant}", ""]
            variant_stats: dict[str, Any] = {}
            for group_name, source_events in analyses:
                variant_events = [dict(event) for event in source_events if include_excluded_day or str(event["occurred_at"])[:10] != EXCLUDED_DAY]
                if mode == "paper":
                    selected, holiday_excluded = exclude_holiday_events(variant_events)
                else:
                    selected, holiday_excluded = variant_events, []
                enrich_events(selected, cache, returns_by_symbol, index_returns)
                exposure_fields = [*(f"corr_{symbol}" for symbol in INDEX_SYMBOLS), "turnover"]
                assign_exposure_buckets(selected, exposure_fields)
                excluded_counts = Counter(event["same_day_exclusion"] for event in selected if event["same_day_exclusion"])
                same_day_events = [event for event in selected if not event["same_day_exclusion"]]
                source_counts = same_day_source_counts(same_day_events)
                holiday_excluded_by_type = dict(Counter(event["event_type"] for event in holiday_excluded))
                index_comparison = summarize_index_bucket_disagreement(selected)
                danger_days = len({str(event["occurred_at"])[:10] for event in selected if event["event_type"] == EVENT_TYPES[0]})
                flags_not_verifiable = len(selected) if not quality_columns[mode] else 0
                analysis_key = f"{mode}/{variant}/{group_name}"
                markdown += [
                    f"### {group_name} (銘柄×日付ユニーク {len(selected)}件)",
                    f"休場日除外: {len(holiday_excluded)}件" + (f" ({', '.join(f'{kind} {count}' for kind, count in sorted(holiday_excluded_by_type.items()))})" if holiday_excluded_by_type else ""),
                    f"MARKET_REGIME_DANGER_SKIP発生日数: {danger_days}日。same-day除外: DAILY_REPLAY_NO_INTRADAY {excluded_counts[DAILY_REPLAY_FLAG]}件、BOARD_UNAVAILABLE {excluded_counts[BOARD_UNAVAILABLE_FLAG]}件。",
                    "当日損益出典: " + (", ".join(f"{key} {value}件" for key, value in sorted(source_counts.items())) if source_counts else "対象なし") + ".",
                    "相関区分の指数間比較: "
                    f"3指数算出可{index_comparison['all_three_available']}件、全指数同じ3分位{index_comparison['same_tercile_all_three']}件、"
                    f"指数間で分位が異なる{index_comparison['different_terciles']}件、少なくとも1指数算出不能{index_comparison['at_least_one_unavailable']}件。",
                ]
                if flags_not_verifiable:
                    markdown.append(
                        f"注意: backtest DBにsame_day_data_quality列がなく、{flags_not_verifiable}件は日足再生・板 unavailable 印の有無をDBから検証できません。明示フラグのみを除外し、00:00時刻だけから印を推定していません。"
                    )
                markdown.append("")

                group_stats: dict[str, Any] = {
                    "n_unique": len(selected),
                    "holiday_excluded_count": len(holiday_excluded),
                    "holiday_excluded_by_type": holiday_excluded_by_type,
                    "danger_skip_days": danger_days,
                    "same_day_excluded": dict(excluded_counts),
                    "same_day_source_counts": dict(source_counts),
                    "index_bucket_comparison": index_comparison,
                    "flags_not_verifiable": flags_not_verifiable,
                    "tables": {},
                }
                for basis, basis_name, basis_events in (
                    ("current", "現行5営業日基準", selected),
                    ("same_day", "当日基準", same_day_events),
                ):
                    markdown += [f"#### {basis_name}", ""]
                    basis_tables = {}
                    for table_name, field, labels in _table_specs():
                        rows = grouped_summary_rows(basis_events, field, basis, labels)
                        basis_tables[table_name] = rows
                        markdown += [f"**{table_name}**", "", *render_table(rows), ""]
                    group_stats["tables"][basis] = basis_tables

                variant_stats[group_name] = group_stats
                report["analyses"][analysis_key] = group_stats
                for event in selected:
                    csv_rows.append({
                        "scope": mode,
                        "variant": variant,
                        "event_group": group_name,
                        "event_type": event["event_type"],
                        "symbol": event["symbol"],
                        "occurred_at": event["occurred_at"],
                        "status": event["status"],
                        "five_day_outcome": event.get("outcome"),
                        "five_day_pnl": event.get("hypothetical_pnl_before_cost"),
                        "same_day_outcome": event["same_day_outcome"],
                        "same_day_pnl": event["same_day_pnl"],
                        "same_day_source": event["same_day_source"],
                        "same_day_unavailable_reason": event["same_day_unavailable_reason"],
                        "same_day_exclusion": event["same_day_exclusion"],
                        "same_day_data_quality": event.get("same_day_data_quality"),
                        **{key: event.get(key) for symbol in INDEX_SYMBOLS for key in (
                            f"corr_{symbol}", f"beta_{symbol}", f"corr_obs_{symbol}", f"corr_{symbol}_bucket",
                            f"combo_{symbol}_bucket",
                        )},
                        "turnover": event.get("turnover"),
                        "turnover_bucket": event["turnover_bucket"],
                    })
            report["analyses"][f"{mode}/{variant}"] = variant_stats

    markdown += [
        "## 前回結果との差",
        previous_report_comparison(report),
        "今回の当日基準はsame_day_close_price→分足B→日足Aの優先選択で1イベント1結果に統一しました。前回のA/B別表とは算出可能件数・対象集合が異なるため、損益額を単純差分として解釈しません。",
        "",
        "## 限界",
        "- サンプルは小さく、3分位および3×3のセルは少数になりやすい。n<5のセルでは割合を出していない。",
        "- DANGER_SKIPが発生した日数が限られ、同じ市場日内の複数銘柄は独立観測ではない。",
        "- 60日相関は短い日次系列からの推定でノイズがあり、指数間の相関・区分も一致しない場合がある。",
        "- backtest DBの現行スキーマにはsame_day_data_qualityがなく、そこに保存されていない除外印は事後確認できない。明示印なしを00:00時刻から推測していない。",
        "- 概算損益は参照価格・数量を用いた仮想値で、手数料、スリッページ、約定可能性を含む実取引損益ではない。",
        "- 相関・売買代金の区分は各集計母集団内で順位により割り当てた記述用区分であり、統計的検定ではない。",
        "",
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "skip_correlation_turnover_report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "skip_correlation_turnover_diagnostics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    csv_fields = list(csv_rows[0]) if csv_rows else ["scope", "variant", "event_group"]
    with (OUTPUT_DIR / "skip_correlation_turnover_events.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(csv_rows)
    print("\n".join(markdown))


if __name__ == "__main__":
    main()