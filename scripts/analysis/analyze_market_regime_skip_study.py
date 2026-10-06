from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time
from pathlib import Path

import pyarrow.parquet as parquet

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.config import config
from src.domain.volatility import VolatilityLevel, resolve_atr_exit_multiplier

DATABASE_PATH = ROOT / "data" / "state" / "filter_decision_events.sqlite3"
PARQUET_ROOT = ROOT / "data" / "minute_bars_parquet"
INDEX_CACHE_PATH = ROOT / "data" / "cache" / "yahoo_daily" / "^N225.json"
START_DATE = "2026-09-15"
STUDY_DATE = "2026-10-06"
EXIT_CUTOFF = time(15, 20)
EVENT_TYPES = ("MARKET_REGIME_DANGER_SKIP", "MARKET_REGIME_CAUTION_RSI_FILTER")


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _load_events() -> list[dict]:
    connection = _connect_read_only(DATABASE_PATH)
    try:
        rows = connection.execute(
            """
            SELECT * FROM filter_decision_events
            WHERE event_type IN (?, ?) AND execution_mode = 'paper'
              AND substr(occurred_at, 1, 10) >= ?
            ORDER BY occurred_at, symbol, id
            """,
            (*EVENT_TYPES, START_DATE),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _load_prices(symbol: str, day: str) -> list[dict]:
    path = PARQUET_ROOT / f"symbol={symbol}" / f"date={day}" / "data.parquet"
    if not path.is_file():
        return []
    table = parquet.read_table(path, columns=["time", "price"])
    rows = [
        {"time": _parse_datetime(row["time"]), "price": float(row["price"])}
        for row in table.to_pylist()
        if row["time"] and row["price"] is not None
    ]
    return sorted(rows, key=lambda row: row["time"])


def _same_day_summary(event: dict) -> dict | None:
    if event.get("same_day_finalized_at") is None or event.get("same_day_close_price") is None:
        return None
    return {
        "source": "DB同日確定集計",
        "high": event.get("same_day_high"),
        "low": event.get("same_day_low"),
        "last": event.get("same_day_close_price"),
        "last_at": event.get("same_day_last_observed_at"),
        "price_1520": event.get("same_day_close_price"),
        "price_1520_at": event.get("same_day_last_observed_at"),
        "pnl_1520": event.get("same_day_hypothetical_pnl_before_cost"),
        "quality": event.get("same_day_data_quality") or "不明",
        "bars": [],
    }


def _bar_summary(event: dict, bars: list[dict]) -> dict | None:
    occurred = _parse_datetime(event["occurred_at"])
    day = event["occurred_at"][:10]
    after = [row for row in bars if row["time"] > occurred]
    if not after:
        return None
    reference = float(event["reference_price"])
    cutoff = datetime.combine(occurred.date(), EXIT_CUTOFF)
    before_exit = [row for row in after if row["time"] <= cutoff]
    exit_row = before_exit[-1] if before_exit else None
    last_row = after[-1]
    exit_price = float(exit_row["price"]) if exit_row else None
    quantity = int(event["quantity"])
    pnl = (exit_price - reference) * quantity if exit_price is not None else None
    return {
        "source": "分足Parquet価格サンプル",
        "high": max(reference, *(float(row["price"]) for row in after)),
        "low": min(reference, *(float(row["price"]) for row in after)),
        "last": float(last_row["price"]),
        "last_at": last_row["time"].isoformat(timespec="seconds"),
        "price_1520": exit_price,
        "price_1520_at": exit_row["time"].isoformat(timespec="seconds") if exit_row else None,
        "pnl_1520": pnl,
        "quality": "価格サンプル。OHLCではない",
        "bars": after,
    }


def _atr_scenario(event: dict, summary: dict | None) -> dict:
    atr = event.get("atr")
    level_value = event.get("atr_level")
    if atr is None or float(atr) <= 0 or level_value not in VolatilityLevel._value2member_map_:
        return {"status": "ATR未記録", "exit_price": None, "exit_at": None, "stop_line": None}

    level = VolatilityLevel(level_value)
    reference = float(event["reference_price"])
    atr = float(atr)
    values = (
        config.ATR_STOP_NORMAL_MULTIPLIER,
        config.ATR_STOP_CAUTION_MULTIPLIER,
        config.ATR_STOP_DANGER_MULTIPLIER,
        config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER,
        config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
        config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
        config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE,
    )

    if summary and summary["bars"]:
        high_water = reference
        cutoff = datetime.combine(_parse_datetime(event["occurred_at"]).date(), EXIT_CUTOFF)
        for row in summary["bars"]:
            if row["time"] > cutoff:
                break
            price = float(row["price"])
            high_water = max(high_water, price)
            multiplier = resolve_atr_exit_multiplier(
                high_water - reference, atr, level, *values
            )
            stop_line = high_water - atr * multiplier
            if price <= stop_line:
                return {
                    "status": "ATR到達(価格サンプル約定)",
                    "exit_price": price,
                    "exit_at": row["time"].isoformat(timespec="seconds"),
                    "stop_line": stop_line,
                }
        multiplier = resolve_atr_exit_multiplier(
            high_water - reference, atr, level, *values
        )
        return {
            "status": "15:20まで未到達",
            "exit_price": None,
            "exit_at": None,
            "stop_line": high_water - atr * multiplier,
        }

    if summary and summary["high"] is not None and summary["low"] is not None:
        high = float(summary["high"])
        low = float(summary["low"])
        if high <= reference:
            multiplier = resolve_atr_exit_multiplier(0.0, atr, level, *values)
            stop_line = reference - atr * multiplier
            if low > stop_line:
                return {
                    "status": "集計高安上は未到達(経路なし)",
                    "exit_price": None,
                    "exit_at": None,
                    "stop_line": stop_line,
                }
    return {"status": "分足経路不足で判定不可", "exit_price": None, "exit_at": None, "stop_line": None}


def _format_number(value: object, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "不明"
    return f"{float(value):,.{digits}f}{suffix}"


def _outcome(pnl: float | None) -> str:
    if pnl is None:
        return "判定不可"
    if pnl > 0:
        return "取り逃し"
    if pnl < 0:
        return "損失回避"
    return "値動きなし"


def _enrich_events(events: list[dict]) -> list[dict]:
    enriched = []
    for event in events:
        day = event["occurred_at"][:10]
        bars = _load_prices(str(event["symbol"]), day)
        summary = _same_day_summary(event) or _bar_summary(event, bars)
        reference = float(event["reference_price"])
        if summary:
            high = float(summary["high"]) if summary["high"] is not None else reference
            low = float(summary["low"]) if summary["low"] is not None else reference
            summary["max_up_pct"] = (high / reference - 1.0) * 100.0
            summary["max_down_pct"] = (low / reference - 1.0) * 100.0
            summary["outcome"] = _outcome(summary["pnl_1520"])
            if summary["price_1520_at"]:
                exit_at = _parse_datetime(summary["price_1520_at"])
                summary["minutes_stale_at_1520"] = (
                    datetime.combine(exit_at.date(), EXIT_CUTOFF) - exit_at
                ).total_seconds() / 60.0
            else:
                summary["minutes_stale_at_1520"] = None
        atr_scenario = _atr_scenario(event, summary)
        atr_scenario["pnl"] = (
            (float(atr_scenario["exit_price"]) - reference) * int(event["quantity"])
            if atr_scenario["exit_price"] is not None
            else None
        )
        enriched.append({**event, "summary": summary, "atr_scenario": atr_scenario})
    return enriched


def _proxy_groups(events: list[dict]) -> list[dict]:
    groups = {"±0.5%以内(直近確定日比 proxy)": [], "±0.5%超(直近確定日比 proxy)": [], "指数入力なし": []}
    for event in events:
        change = event.get("nikkei_change_percent")
        key = "指数入力なし" if change is None else (
            "±0.5%以内(直近確定日比 proxy)" if abs(float(change)) <= 0.5
            else "±0.5%超(直近確定日比 proxy)"
        )
        groups[key].append(event)

    output = []
    for label, rows in groups.items():
        outcomes = [row["summary"]["pnl_1520"] for row in rows if row["summary"] is not None]
        output.append({
            "label": label,
            "events": len(rows),
            "missed": sum(value is not None and value > 0 for value in outcomes),
            "missed_amount": sum(value for value in outcomes if value is not None and value > 0),
            "avoided": sum(value is not None and value < 0 for value in outcomes),
            "avoided_amount": -sum(value for value in outcomes if value is not None and value < 0),
            "flat": sum(value == 0 for value in outcomes),
            "unavailable": sum(value is None for value in outcomes) + sum(
                row["summary"] is None for row in rows
            ),
            "net_pnl": sum(value for value in outcomes if value is not None),
        })
    return output


def _index_cache_range() -> tuple[str | None, str | None, int]:
    if not INDEX_CACHE_PATH.is_file():
        return None, None, 0
    cache = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
    dates = sorted(cache)
    return (dates[0], dates[-1], len(dates)) if dates else (None, None, 0)


def _event_row(event: dict) -> str:
    summary = event["summary"]
    occurred = event["occurred_at"]
    if summary is None:
        movement = "分足なし・同日確定値なし"
        exit_value = "不明"
        outcome = "判定不可"
    else:
        movement = (
            f"{_format_number(summary['high'])}/{_format_number(summary['low'])}/"
            f"{_format_number(summary['last'])}"
        )
        exit_value = (
            f"{_format_number(summary['price_1520'])}"
            f" ({summary['price_1520_at'] or '時刻不明'}) / "
            f"{_format_number(summary['pnl_1520'], 0, '円')}"
        )
        outcome = summary["outcome"]
    n225 = _format_number(event.get("nikkei_change_percent"), 3, "%")
    atr_result = event["atr_scenario"]
    atr_text = atr_result["status"]
    if atr_result["stop_line"] is not None:
        atr_text += f"(線={_format_number(atr_result['stop_line'])})"
    stale = (
        f"{_format_number(summary['minutes_stale_at_1520'], 0, '分')}"
        if summary and summary["minutes_stale_at_1520"] is not None else "不明"
    )
    movement += f" / 上{_format_number(summary['max_up_pct'], 2, '%')} 下{_format_number(summary['max_down_pct'], 2, '%')}" if summary else ""
    return (
        f"| {occurred[:16].replace('T', ' ')} | {event['symbol']} | {event['event_type']} | "
        f"{_format_number(event['reference_price'])} | {n225} | {movement} | "
        f"{exit_value} (15:20までの最終観測から{stale}) | {atr_text} | {outcome} |"
    )


def build_report() -> str:
    events = _enrich_events(_load_events())
    today_events = [event for event in events if event["occurred_at"][:10] == STUDY_DATE]
    matched_bars = sum(
        _load_prices(str(event["symbol"]), event["occurred_at"][:10]) != []
        for event in events
    )
    summaries = [event["summary"] for event in events if event["summary"] is not None]
    outcome_counts = {name: 0 for name in ("取り逃し", "損失回避", "値動きなし", "判定不可")}
    missed_amount = 0.0
    avoided_amount = 0.0
    for event in events:
        summary = event["summary"]
        result = summary["outcome"] if summary else "判定不可"
        outcome_counts[result] += 1
        if summary and summary["pnl_1520"] is not None:
            missed_amount += max(0.0, float(summary["pnl_1520"]))
            avoided_amount += max(0.0, -float(summary["pnl_1520"]))

    first_index_date, last_index_date, index_day_count = _index_cache_range()
    n225_minute_files = list((PARQUET_ROOT / "symbol=^N225").glob("date=*/data.parquet"))
    proxy_groups = _proxy_groups(events)
    multipliers = (
        f"NORMAL={config.ATR_STOP_NORMAL_MULTIPLIER:g}, "
        f"CAUTION={config.ATR_STOP_CAUTION_MULTIPLIER:g}, "
        f"DANGER={config.ATR_STOP_DANGER_MULTIPLIER:g}"
    )
    profit_lock = (
        f"NORMAL={config.ATR_PROFIT_LOCK_NORMAL_MULTIPLIER:g}, "
        f"CAUTION={config.ATR_PROFIT_LOCK_CAUTION_MULTIPLIER:g}, "
        f"DANGER={config.ATR_PROFIT_LOCK_DANGER_MULTIPLIER:g}, "
        f"trigger={config.ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE:g}ATR"
    )

    lines = [
        "# MarketRegime見送り・場中緩和の材料調査",
        "",
        f"作成日: {STUDY_DATE}。対象はpaper本番イベントDB、開始日{START_DATE}。DBはSQLite URI `mode=ro`で参照。",
        "",
        "## 要点",
        f"- 10/6の対象イベントはDB上1件のみ（{', '.join(str(event['symbol']) for event in today_events) or 'なし'}）。依頼にある3銘柄の残り2件は、対象イベントDB・10/6取引ログで特定できない。",
        f"- 9/15以降の対象イベントは{len(events)}件。銘柄・日付一致の分足Parquetは{matched_bars}件、10/5・10/6の3件はDBの同日集計を利用。9/21の1件は同日価格データなし。",
        f"- 15:20基準の利用可能結果: 取り逃し{outcome_counts['取り逃し']}件/{missed_amount:,.0f}円、損失回避{outcome_counts['損失回避']}件/{avoided_amount:,.0f}円、値動きなし{outcome_counts['値動きなし']}件、判定不可{outcome_counts['判定不可']}件。売買コスト前の概算。",
        "- 同日N225の分足/時点値は保存されていない。イベントDBの`nikkei_change_percent`はMarketRegimeが用いる直近確定日足の前日比で、見送り時刻の当日比ではない。",
        "",
        "## Part A: 2026-10-06",
        "10/6ログのMarketRegime記録はCAUTION基準引き上げ（8944）の反復で、見送り理由コードではない。イベントDB上の当日見送りは次の1件。",
        "",
        "| 時刻 | 銘柄 | 理由コード | 見送り価格 | 見送り後 高値/安値/最終観測 | 最大上昇/下落 | 15:20基準価格・概算損益 | ATRトレーリング | N225当日比 |",
        "|---|---:|---|---:|---|---|---|---|---|",
    ]
    for event in today_events:
        summary = event["summary"]
        if summary is None:
            movement = "不明"
            change = "不明"
            exit_value = "不明"
        else:
            movement = (
                f"{_format_number(summary['high'])}/{_format_number(summary['low'])}/"
                f"{_format_number(summary['last'])} @ {summary['last_at']}"
            )
            change = f"+{_format_number(summary['max_up_pct'], 2, '%')} / {_format_number(summary['max_down_pct'], 2, '%')}"
            exit_value = (
                f"{_format_number(summary['price_1520'])} @ {summary['price_1520_at']} / "
                f"{_format_number(summary['pnl_1520'], 0, '円')}"
            )
        atr_result = event["atr_scenario"]
        atr_text = atr_result["status"]
        if atr_result["stop_line"] is not None:
            atr_text += f"(線={_format_number(atr_result['stop_line'])})"
        lines.append(
            f"| {event['occurred_at'][11:19]} | {event['symbol']} | {event['event_type']} | "
            f"{_format_number(event['reference_price'])}円 | {movement} | {change} | "
            f"{exit_value} | {atr_text} | 当日比未保存（保存入力={_format_number(event.get('nikkei_change_percent'), 3, '%')}は直近確定日足比） |"
        )
    missing_today = max(0, 3 - len(today_events))
    lines.extend([
        "",
        f"3件という前提に対し、記録で確認できるのは{len(today_events)}件、残り{missing_today}件は銘柄・時刻・理由・価格とも未特定。10/5には4833・8918の2件があるが、別日なので10/6の3件には数えていない。",
        "10/6の4597は見送り32円、ATR=1.714円/NORMAL。現在設定のNORMAL倍率での初期ストップ線は29.43円、同日高安は32/31円なのでATR線未到達。15:20基準の最終観測31円（15:19:18）で-900円。DB確定時刻は15:20:22、データ品質OKだが公式15:30終値ではない。",
        "",
        "## Part B: 全イベント",
        "高値/安値/最終観測は見送り時刻より後の保存値。ParquetはOHLCではなく分単位の`price`サンプルなので、表示極値は保存サンプル内の極値。15:20価格は15:20以前の最後のサンプル（またはDB同日確定値）。金額は記録quantityをそのまま使い、手数料・約定滑りを除く。",
        f"ATR試算は実行時config値（損切り倍率: {multipliers}、利益ロック倍率: {profit_lock}）を使用。イベント時点の倍率自体は保存されていないため、過去設定と一致する保証はない。",
        "",
        "| 時刻 | 銘柄 | 理由コード | 見送り価格 | 保存N225値* | 高値/安値/最終観測 / 最大上昇・下落 | 15:20価格・概算損益 | ATR決済概算 | 結果 |",
        "|---|---:|---|---:|---:|---|---|---|---|",
    ])
    lines.extend(_event_row(event) for event in events)
    lines.extend([
        "",
        "* 保存N225値はイベントDBの`nikkei_change_percent`。MarketRegimeコード上、日経の日足終値から計算した直近確定日の前日比であり、当日場中の値ではない。判定時点で利用可能な古い値ではあるが、イベント時の同日値動きの代理にはしない。",
        "",
        "### 当日比群の集計可否と保存値proxy",
        "依頼どおりの「見送り時刻の日経当日前日比」による群分けは不可（指数分足なし）。参考として、先読みを避けイベントに記録済みの直近確定日足比だけで±0.5%群を作ると以下。ただしこれは同日場中の群ではなく、結論には使わない。",
        "",
        "| 保存値proxy群 | イベント数 | 取り逃し件数/金額 | 損失回避件数/金額 | 値動きなし | 結果不明 | 損益合計 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for group in proxy_groups:
        lines.append(
            f"| {group['label']} | {group['events']} | {group['missed']}/{group['missed_amount']:,.0f}円 | "
            f"{group['avoided']}/{group['avoided_amount']:,.0f}円 | {group['flat']} | {group['unavailable']} | {group['net_pnl']:,.0f}円 |"
        )
    lines.extend([
        "",
        "保存値proxyの±0.5%以内は1件で、取り逃し0・損失回避1件（200円）。件数が1件のため、落ち着いた日ほど取り逃しが多いかはサンプル不足。実際の同日N225群ではない点もあり、緩和根拠にはできない。",
        "",
        "### 日経225の時点再現性",
        f"- `data/minute_bars_parquet`には^N225の日中Parquetが{len(n225_minute_files)}件。ローカル日足キャッシュは{index_day_count}日分、{first_index_date or '不明'}〜{last_index_date or '不明'}までで、10/5・10/6の日足も含まれない。",
        "- `MarketRegimeUseCase`は日経の日足終値系列に`calculate_previous_day_changes`を適用し、`YahooIndexClient.get_daily_ohlc`は当日分（JST日付が今日以上）を除外する。したがってイベント列に保存されたN225値は当日場中比ではない。",
        "- 見送り時点を再現するには、イベント発生時に^N225の1分足を取得し、イベント時刻以前の直近価格・直近営業日終値・計算比率・取得時刻・元データ時刻を同じイベント行へ保存する必要がある。既存の`YahooIndexClient.get_intraday_change_percent`はYahoo 1分足（range=5d）取得に対応するが、過去分は保持期間を過ぎると再取得できないため、今後分は当時保存が必要。",
        "",
        "### 先読み確認",
        "- 群分けproxyにはイベント時に保存された`nikkei_change_percent`だけを使用。日経の当日終値やイベント後の指数値で群を決めていない。",
        "- イベント後の銘柄価格は損益結果の算定だけに使用し、群分けには使用していない。日経当日終値ベースの既存診断値は本集計の分類に使っていない。",
        "- 当日価格の分足欠損日は、既存DBの同日確定列を優先。9/21は同日価格も保存されていないため、結果不明として残した。",
        "",
        "## 判断",
        "途中更新を検討する価値は、現時点では判断保留（サンプル不足）。",
        f"当日確認できた1件は損失回避の概算{abs(float(today_events[0]['summary']['pnl_1520'])):,.0f}円で、しかも15:19までの値。残り2件の記録がなく、当日3件の比較はできない。" if today_events and today_events[0]["summary"] and today_events[0]["summary"]["pnl_1520"] is not None else "10/6の同日結果は記録不足で、当日比較はできない。",
        "Parquet一致9件では取り逃し1件(+100円)、損失回避4件(1,500円)、値動きなし4件。指数群は同日値で作れず、唯一の落ち着きproxy群も1件だけ。",
        "ATR列が欠ける過去イベントが多く、10/5・10/6以外にも同日分足が欠ける日がある。ATR出口とサンプルの代表性は限定的。",
        "緩和判断より先に、今後の見送りイベントへ時点N225スナップショットを記録し、追加標本を蓄積するのが妥当。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="MarketRegime見送りの当日推移を読み取り専用で集計")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "analysis" / "market_regime_skip_study_20261006.md",
    )
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"report={args.output}")
    print(f"events={len(_load_events())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())