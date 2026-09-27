"""週次ATR/市場レジーム見送りイベントの深刻度を比較する読み取り専用調査スクリプト。

実行方法:
    python scripts/analysis/analyze_regime_skip_severity.py --start-date 2026-09-22 --end-date 2026-09-25

filter_decision_eventsは観測時点のスナップショットです。MARKET_REGIME_*系の値は
イベント発生時点の値で、日中に複数回更新される可能性があります。日別サマリーは
同日・同イベント種別・同じ観測値を重複排除した後、最後に記録された値を代表値とします。
この診断は閾値の妥当性を判断する材料であり、結果だけを根拠に閾値を自動変更せず、
診断・人間の判断・必要に応じた別途変更の順で扱ってください。
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config  # noqa: E402
from src.infrastructure.calendar.japanese_calendar import is_trading_day  # noqa: E402


ATR_BORDERLINE_UPPER_MULTIPLE = 1.1
ATR_CLEAR_DANGER_MULTIPLE = 1.5
EVENT_TYPES = (
    "ATR_DANGER_SKIP",
    "MARKET_REGIME_DANGER_SKIP",
    "MARKET_REGIME_CAUTION_RSI_FILTER",
)
MARKET_EVENT_TYPES = EVENT_TYPES[1:]
MARKET_FIELDS = (
    "realized_volatility_percent",
    "vix",
    "nikkei_change_percent",
    "adx",
)


def classify_atr_severity(ratio: float | None, danger_threshold: float) -> str:
    """ATR危険閾値に対する比率の区分を返す。"""
    if ratio is None or not math.isfinite(ratio):
        return "データなし"
    if ratio < danger_threshold:
        return "閾値未満"
    if ratio < danger_threshold * ATR_BORDERLINE_UPPER_MULTIPLE:
        return "ボーダーライン"
    if ratio >= danger_threshold * ATR_CLEAR_DANGER_MULTIPLE:
        return "明確な危険域"
    return "中間域"


def _parse_date(value: str) -> date:
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("YYYY-MM-DD形式の日付を指定してください") from exc


def _periods(start: date, end: date) -> tuple[list[date], list[date]]:
    if start > end:
        raise ValueError("--start-dateは--end-date以前の日付を指定してください")

    target_days: list[date] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            target_days.append(current)
        current += timedelta(days=1)
    if not target_days:
        raise ValueError("対象期間に日本市場の営業日がありません")

    reference_days: list[date] = []
    current = start - timedelta(days=1)
    while len(reference_days) < len(target_days):
        if is_trading_day(current):
            reference_days.append(current)
        current -= timedelta(days=1)
    reference_days.reverse()
    return target_days, reference_days


def load_events(database_path: Path, trading_days: list[date]) -> list[dict[str, Any]]:
    """期間内の対象イベントをSQLiteの読み取り専用接続で取得する。"""
    if not trading_days:
        return []
    if not database_path.exists():
        raise FileNotFoundError(f"判定イベントDBが見つかりません: {database_path}")

    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    day_placeholders = ", ".join("?" for _ in trading_days)
    query = """
        SELECT event_type, symbol, execution_mode, occurred_at, atr, true_range,
               atr_ratio, atr_level, market_regime, realized_volatility_percent,
               vix, nikkei_change_percent, adx
        FROM filter_decision_events
        WHERE event_type IN (?, ?, ?)
          AND substr(occurred_at, 1, 10) IN ({day_placeholders})
        ORDER BY occurred_at, symbol, execution_mode
    """.format(day_placeholders=day_placeholders)
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            query,
            (*EVENT_TYPES, *(trading_day.isoformat() for trading_day in trading_days)),
        ).fetchall()
    return [dict(row) for row in rows]


def _finite_values(events: list[dict[str, Any]], field: str) -> list[float]:
    values = []
    for event in events:
        value = event.get(field)
        if value is not None and math.isfinite(float(value)):
            values.append(float(value))
    return values


def _mode_counts(events: list[dict[str, Any]]) -> str:
    counts = Counter(str(event.get("execution_mode") or "不明") for event in events)
    return ", ".join(f"{mode}:{count}" for mode, count in sorted(counts.items())) or "なし"


def _print_atr_summary(events: list[dict[str, Any]]) -> None:
    atr_events = [event for event in events if event["event_type"] == "ATR_DANGER_SKIP"]
    ratios = _finite_values(atr_events, "atr_ratio")
    threshold = config.ATR_DANGER_RATIO
    categories = Counter(classify_atr_severity(value, threshold) for value in ratios)

    print("【ATR_DANGER_SKIP】")
    print(f"イベント: {len(atr_events)}件 (実行モード: {_mode_counts(atr_events)})")
    print(f"atr_ratio有効値: {len(ratios)}件 / 閾値: {threshold:.3f}")
    if ratios:
        print(
            f"分布: 最小 {min(ratios):.3f} / 最大 {max(ratios):.3f} / "
            f"平均 {statistics.mean(ratios):.3f} / 中央値 {statistics.median(ratios):.3f}"
        )
    print(
        "深刻度: "
        f"ボーダーライン({threshold:.3f}以上、{threshold * ATR_BORDERLINE_UPPER_MULTIPLE:.3f}未満) "
        f"{categories['ボーダーライン']}件 / "
        f"中間域 {categories['中間域']}件 / "
        f"明確な危険域({threshold * ATR_CLEAR_DANGER_MULTIPLE:.3f}以上) "
        f"{categories['明確な危険域']}件 / 閾値未満 {categories['閾値未満']}件 / "
        f"データなし {len(atr_events) - len(ratios)}件"
    )

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in atr_events:
        by_day[str(event["occurred_at"])[:10]].append(event)
        by_symbol[str(event["symbol"])].append(event)

    print("日別内訳 (件数 / ratio中央値 / ボーダーライン / 明確な危険域):")
    for event_day, daily_events in sorted(by_day.items()):
        daily_ratios = _finite_values(daily_events, "atr_ratio")
        daily_categories = Counter(classify_atr_severity(value, threshold) for value in daily_ratios)
        median = f"{statistics.median(daily_ratios):.3f}" if daily_ratios else "なし"
        print(
            f"- {event_day}: {len(daily_events)}件 / {median} / "
            f"{daily_categories['ボーダーライン']}件 / {daily_categories['明確な危険域']}件"
        )

    print("銘柄別内訳 (件数 / ratio平均 / ボーダーライン / 明確な危険域):")
    for symbol, symbol_events in sorted(by_symbol.items()):
        symbol_ratios = _finite_values(symbol_events, "atr_ratio")
        symbol_categories = Counter(classify_atr_severity(value, threshold) for value in symbol_ratios)
        mean = f"{statistics.mean(symbol_ratios):.3f}" if symbol_ratios else "なし"
        print(
            f"- {symbol}: {len(symbol_events)}件 / {mean} / "
            f"{symbol_categories['ボーダーライン']}件 / {symbol_categories['明確な危険域']}件"
        )


def _market_thresholds(event_type: str) -> dict[str, float]:
    danger = event_type == "MARKET_REGIME_DANGER_SKIP"
    regime_thresholds = config.MARKET_REGIME_THRESHOLDS
    return {
        "realized_volatility_percent": (
            regime_thresholds.realized_vol_danger if danger else regime_thresholds.realized_vol_caution
        ),
        "vix": regime_thresholds.vix_danger if danger else regime_thresholds.vix_caution,
        "nikkei_change_percent": regime_thresholds.nikkei_change_upgrade,
        "adx": config.MARKET_REGIME_ADX_TREND_THRESHOLD,
    }


def _deduplicate_market_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """同日・同種別で同じレジーム値を持つ銘柄別重複行を1件にする。"""
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in events:
        key = (
            event["event_type"],
            str(event["occurred_at"])[:10],
            event.get("market_regime"),
            *(event.get(field) for field in MARKET_FIELDS),
        )
        current = unique.get(key)
        if current is None or event["occurred_at"] > current["occurred_at"]:
            unique[key] = event

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in unique.values():
        by_type[str(event["event_type"])].append(event)
    for rows in by_type.values():
        rows.sort(key=lambda event: (str(event["occurred_at"])[:10], event["occurred_at"]))
    return by_type


def _print_market_summary(events: list[dict[str, Any]]) -> None:
    print("【MARKET_REGIME系イベント】")
    for event_type in MARKET_EVENT_TYPES:
        raw_events = [event for event in events if event["event_type"] == event_type]
        unique_by_type = _deduplicate_market_events(raw_events).get(event_type, [])
        thresholds = _market_thresholds(event_type)
        label = "DANGER_SKIP" if event_type == "MARKET_REGIME_DANGER_SKIP" else "CAUTION_RSI_FILTER"
        print(
            f"{label}: 元イベント {len(raw_events)}件 / 日別ユニーク観測値 {len(unique_by_type)}件 "
            f"(実行モード: {_mode_counts(raw_events)})"
        )
        by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in unique_by_type:
            by_day[str(event["occurred_at"])[:10]].append(event)

        for event_day, daily_events in sorted(by_day.items()):
            representative = max(daily_events, key=lambda event: event["occurred_at"])
            values = []
            for field in MARKET_FIELDS:
                value = representative.get(field)
                threshold = thresholds[field]
                if value is None:
                    values.append(f"{field}=欠損")
                    continue
                value = float(value)
                difference = abs(value) - threshold if field == "nikkei_change_percent" else value - threshold
                values.append(f"{field}={value:.3f} (基準差 {difference:+.3f})")
            print(
                f"- {event_day}: {len(daily_events)}観測値, 代表 {representative['occurred_at']} "
                f"regime={representative.get('market_regime') or '不明'}; " + "; ".join(values)
            )


def print_period(label: str, start: date, end: date, trading_days: list[date], events: list[dict[str, Any]]) -> None:
    print(f"\n===== {label}: {start.isoformat()} ～ {end.isoformat()} ({len(trading_days)}営業日) =====")
    print("イベント件数: " + " / ".join(
        f"{event_type} {sum(event['event_type'] == event_type for event in events)}件"
        for event_type in EVENT_TYPES
    ))
    _print_atr_summary(events)
    _print_market_summary(events)


def main() -> None:
    parser = argparse.ArgumentParser(description="週次ATR/市場レジーム見送りイベントの深刻度を比較します")
    parser.add_argument("--start-date", required=True, type=_parse_date, help="対象期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=True, type=_parse_date, help="対象期間の終了日 (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        target_days, reference_days = _periods(args.start_date, args.end_date)
        reference_start = reference_days[0]
        reference_end = reference_days[-1]
        target_events = load_events(config.FILTER_DECISION_DATABASE_FILE, target_days)
        reference_events = load_events(config.FILTER_DECISION_DATABASE_FILE, reference_days)
    except (ValueError, FileNotFoundError, sqlite3.Error) as exc:
        parser.error(str(exc))

    print("週次ATR/レジームskipイベント深刻度診断")
    print(
        "ATR閾値: "
        f"CAUTION={config.ATR_CAUTION_RATIO:.3f}, DANGER={config.ATR_DANGER_RATIO:.3f}; "
        "市場閾値: "
        f"実現ボラ CAUTION={config.MARKET_REGIME_THRESHOLDS.realized_vol_caution:.3f} / "
        f"DANGER={config.MARKET_REGIME_THRESHOLDS.realized_vol_danger:.3f}, "
        f"VIX CAUTION={config.MARKET_REGIME_THRESHOLDS.vix_caution:.3f} / "
        f"DANGER={config.MARKET_REGIME_THRESHOLDS.vix_danger:.3f}, "
        f"ADXトレンド参考値={config.MARKET_REGIME_ADX_TREND_THRESHOLD:.3f}"
    )
    print("市場系の日別代表値は、重複排除後に最後に記録された観測値です。")
    print_period("対象期間", args.start_date, args.end_date, target_days, target_events)
    print_period("直前の同数営業日", reference_start, reference_end, reference_days, reference_events)
    print(
        "\n【解釈上の注意】診断結果は閾値妥当性の判断材料です。結果だけで自動変更せず、"
        "人が判断した上で必要な場合に別途変更してください。"
    )


if __name__ == "__main__":
    main()