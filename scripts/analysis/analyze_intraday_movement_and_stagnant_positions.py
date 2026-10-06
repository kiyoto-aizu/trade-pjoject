"""保存済み分足から時間帯別値動きと停滞ポジションを調査する読み取り専用診断。"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

ATR_PERIOD = 14
STOP_TIME = time(15, 20)
ELAPSED_MINUTES = (15, 30, 45, 60)
ATR_FRACTIONS = (0.2, 0.3, 0.4)
SPREADS = (0.0, 0.001)
TIME_WINDOWS = (
    ("09:00-09:30", time(9, 0), time(9, 30)),
    ("09:30-10:00", time(9, 30), time(10, 0)),
    ("10:00-10:30", time(10, 0), time(10, 30)),
    ("10:30-11:30", time(10, 30), time(11, 30)),
    ("12:30-13:00", time(12, 30), time(13, 0)),
    ("13:00-14:00", time(13, 0), time(14, 0)),
    ("14:00-14:30", time(14, 0), time(14, 30)),
    ("14:30-15:00", time(14, 30), time(15, 0)),
    ("15:00-15:20", time(15, 0), time(15, 20)),
    ("15:20-15:30", time(15, 20), time(15, 30)),
)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def time_window(value: datetime | time) -> str | None:
    clock = value.time() if isinstance(value, datetime) else value
    for label, start, end in TIME_WINDOWS:
        if start <= clock < end:
            return label
    return None


def atr_from_daily_cache(rows: dict, day: str) -> tuple[float | None, float | None]:
    """既存のATR実装と同じ直近14本TR単純平均と、直近前日終値を返す。"""
    prior = [(date, rows[date]) for date in sorted(rows) if date < day]
    previous_close = float(prior[-1][1]["close"]) if prior else None
    if len(prior) < ATR_PERIOD:
        return None, previous_close
    recent = prior[-ATR_PERIOD:]
    ranges = []
    for index, (_, bar) in enumerate(recent):
        high, low = float(bar["high"]), float(bar["low"])
        prev_close = float(recent[index - 1][1]["close"]) if index else float(bar["close"])
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges) / ATR_PERIOD, previous_close


def is_stagnant(prices: list[float], reference: float, atr_fraction: float) -> bool:
    if not prices or reference <= 0:
        return False
    return max(prices) - min(prices) < reference * atr_fraction


def virtual_pnl(entry_price: float, exit_price: float, quantity: float, spread: float = 0.0) -> float:
    return (exit_price * (1.0 - spread) - entry_price) * quantity


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _load_daily_cache(directory: Path, symbol: str, day: str) -> dict:
    return _load_json(directory / f"{symbol}.json", {})


def _load_bars(path: Path) -> list[dict]:
    if not path.exists():
        return []
    import pyarrow.parquet as parquet

    rows = parquet.read_table(path, columns=["time", "price", "volume"]).to_pylist()
    return sorted(
        ({"time": parse_timestamp(row["time"]), "price": float(row["price"]), "volume": row["volume"]}
         for row in rows),
        key=lambda row: row["time"],
    )


def _partition_path(minute_directory: Path, day: str, symbol: str) -> Path:
    return minute_directory / f"symbol={symbol}" / f"date={day}" / "data.parquet"


def _window_chunks(label: str) -> list[tuple[time, time]]:
    _, start, end = next(item for item in TIME_WINDOWS if item[0] == label)
    if (datetime.combine(datetime.min, end) - datetime.combine(datetime.min, start)).total_seconds() > 1800:
        midpoint = (datetime.combine(datetime.min, start) + timedelta(minutes=30)).time()
        return [(start, midpoint), (midpoint, end)]
    return [(start, end)]


def analyze_day_windows(bars: list[dict], reference: float | None) -> dict[str, dict]:
    """1銘柄日について、30分換算値幅比・出来高シェア・横ばい率を計算する。"""
    in_scope = [bar for bar in bars if time_window(bar["time"]) is not None]
    known_volumes = [float(bar["volume"]) for bar in in_scope if bar["volume"] is not None]
    day_volume = sum(known_volumes)
    by_label: dict[str, list[dict]] = defaultdict(list)
    for bar in in_scope:
        by_label[time_window(bar["time"])].append(bar)

    results = {}
    for label, _, _ in TIME_WINDOWS:
        items = sorted(by_label[label], key=lambda bar: bar["time"])
        if not items:
            continue
        chunk_ranges = []
        for start, end in _window_chunks(label):
            chunk = [bar["price"] for bar in items if start <= bar["time"].time() < end]
            if not chunk:
                continue
            actual_minutes = (datetime.combine(datetime.min, end) - datetime.combine(datetime.min, start)).total_seconds() / 60
            chunk_ranges.append((max(chunk) - min(chunk)) * 30.0 / actual_minutes)
        range_30m = statistics.fmean(chunk_ranges) if chunk_ranges else None

        volumes = [float(bar["volume"]) for bar in items if bar["volume"] is not None]
        comparable = 0
        unchanged = 0
        for previous, current in zip(items, items[1:]):
            if current["time"] - previous["time"] == timedelta(minutes=1):
                comparable += 1
                unchanged += previous["price"] == current["price"]
        results[label] = {
            "range_30m_ratio": range_30m / reference if range_30m is not None and reference else None,
            "volume_share": sum(volumes) / day_volume if day_volume > 0 and volumes else None,
            "unchanged_ratio": unchanged / comparable if comparable else None,
            "bar_count": len(items),
            "volume_bar_count": len(volumes),
            "unchanged_pair_count": comparable,
        }
    return results


def _filled_buy(event: dict, source: str) -> dict | None:
    if str(event.get("side")) != "2":
        return None
    if source == "production" and event.get("result_code") not in (0, "0"):
        return None
    timestamp = event.get("timestamp") or event.get("time")
    if not timestamp:
        return None
    try:
        moment = parse_timestamp(timestamp)
        price = float(event.get("price", event.get("delayed_price")))
        quantity = float(event["qty"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or quantity <= 0:
        return None
    return {**event, "source": source, "moment": moment, "day": moment.date().isoformat(),
            "symbol": str(event["symbol"]), "entry_price": price, "quantity": quantity}


def _trade_positions(events: list[dict], source: str) -> list[dict]:
    buys = [item for event in events if (item := _filled_buy(event, source)) is not None]
    sells = []
    for event in events:
        if str(event.get("side")) != "1":
            continue
        timestamp = event.get("timestamp") or event.get("time")
        try:
            sells.append({**event, "moment": parse_timestamp(timestamp), "symbol": str(event["symbol"]),
                          "price": float(event["price"]), "qty": float(event["qty"]), "remaining": float(event["qty"])})
        except (KeyError, TypeError, ValueError):
            continue
    sells.sort(key=lambda item: item["moment"])
    buys.sort(key=lambda item: item["moment"])
    for buy in buys:
        remaining = buy["quantity"]
        exits = []
        for sell in sells:
            if (sell["symbol"] != buy["symbol"] or sell["moment"] <= buy["moment"]
                    or sell["remaining"] <= 0):
                continue
            matched = min(remaining, sell["remaining"])
            exits.append({"moment": sell["moment"], "price": sell["price"], "quantity": matched})
            sell["remaining"] -= matched
            remaining -= matched
            if remaining <= 0:
                break
        buy["exits"] = exits
        buy["actual_exit_time"] = exits[-1]["moment"] if exits and remaining <= 0 else None
        buy["actual_pnl"] = (
            sum((exit_row["price"] - buy["entry_price"]) * exit_row["quantity"] for exit_row in exits)
            if exits and remaining <= 0 and all(exit_row["price"] > 0 for exit_row in exits)
            else None
        )
        buy["unmatched_quantity"] = remaining
    return buys


def evaluate_position(buy: dict, bars: list[dict], reference: float, elapsed: int) -> dict | None:
    target = buy["moment"] + timedelta(minutes=elapsed)
    cutoff = datetime.combine(buy["moment"].date(), STOP_TIME)
    if target > cutoff:
        return None
    eligible = [bar for bar in bars if target <= bar["time"] <= cutoff]
    if not eligible:
        return None
    evaluation_bar = eligible[0]
    if buy.get("actual_exit_time") and buy["actual_exit_time"] < evaluation_bar["time"]:
        return None
    observed = [buy["entry_price"]] + [bar["price"] for bar in bars if buy["moment"] <= bar["time"] <= evaluation_bar["time"]]
    actual_pnl = buy.get("actual_pnl")
    after = [bar["price"] for bar in bars if evaluation_bar["time"] < bar["time"] <= cutoff]
    exit_price = evaluation_bar["price"]
    return {
        "source": buy["source"], "symbol": buy["symbol"], "day": buy["day"],
        "buy_time": buy["moment"].isoformat(), "evaluation_time": evaluation_bar["time"].isoformat(),
        "elapsed_minutes": elapsed, "entry_price": buy["entry_price"], "quantity": buy["quantity"],
        "observed_range": max(observed) - min(observed), "reference": reference,
        "actual_pnl": actual_pnl, "exit_price": exit_price,
        "virtual_pnl": {str(spread): virtual_pnl(buy["entry_price"], exit_price, buy["quantity"], spread) for spread in SPREADS},
        "max_rise_after": max((price - exit_price for price in after), default=None),
        "max_fall_after": min((price - exit_price for price in after), default=None),
        "other_executed_buys_after": [],
    }


def evaluate_cohort(buys: list[dict], minute_directory: Path, daily_directory: Path,
                    all_buys: list[dict]) -> tuple[list[dict], dict]:
    evaluated = []
    diagnostics = {"buy_count": len(buys), "with_minute_bars": 0, "without_minute_bars": [],
                   "atr_reference_count": 0, "previous_close_fallback_count": 0, "without_reference": [],
                   "actual_pnl_unavailable_count": sum(buy.get("actual_pnl") is None for buy in buys)}
    bars_by_key = {}
    for buy in buys:
        key = (buy["day"], buy["symbol"])
        if key not in bars_by_key:
            bars_by_key[key] = _load_bars(_partition_path(minute_directory, *key))
        bars = bars_by_key[key]
        if not bars:
            diagnostics["without_minute_bars"].append(f"{buy['day']} {buy['symbol']}")
            continue
        diagnostics["with_minute_bars"] += 1
        daily = _load_daily_cache(daily_directory, buy["symbol"], buy["day"])
        atr, prior_close = atr_from_daily_cache(daily, buy["day"])
        recorded_atr = buy.get("atr")
        if isinstance(recorded_atr, (int, float)) and recorded_atr > 0:
            reference, reference_kind = float(recorded_atr), "注文記録ATR"
        elif atr and atr > 0:
            reference, reference_kind = atr, "日足ATR"
        elif prior_close and prior_close > 0:
            reference, reference_kind = prior_close * 0.005, "前日終値0.5%"
        else:
            diagnostics["without_reference"].append(f"{buy['day']} {buy['symbol']}")
            continue
        diagnostics["atr_reference_count" if reference_kind != "前日終値0.5%" else "previous_close_fallback_count"] += 1
        for elapsed in ELAPSED_MINUTES:
            row = evaluate_position(buy, bars, reference, elapsed)
            if row is None:
                continue
            row["reference_kind"] = reference_kind
            for other in all_buys:
                if (other["day"] == buy["day"] and other["symbol"] != buy["symbol"]
                        and other["moment"] > parse_timestamp(row["evaluation_time"])):
                    row["other_executed_buys_after"].append(other["symbol"])
            evaluated.append(row)
    return evaluated, diagnostics


def summarize_stagnation(evaluated: list[dict]) -> list[dict]:
    rows = []
    for elapsed in ELAPSED_MINUTES:
        for fraction in ATR_FRACTIONS:
            selected = [row for row in evaluated if row["elapsed_minutes"] == elapsed
                        and row["observed_range"] < row["reference"] * fraction]
            rows.append({
                "elapsed_minutes": elapsed, "atr_fraction": fraction, "count": len(selected),
                "virtual_pnl_no_spread": sum(row["virtual_pnl"]["0.0"] for row in selected),
                "virtual_pnl_spread": sum(row["virtual_pnl"]["0.001"] for row in selected),
                "actual_difference_no_spread": sum(row["virtual_pnl"]["0.0"] - row["actual_pnl"]
                                                    for row in selected if row["actual_pnl"] is not None),
                "actual_difference_spread": sum(row["virtual_pnl"]["0.001"] - row["actual_pnl"]
                                                 for row in selected if row["actual_pnl"] is not None),
                "actual_pnl_count": sum(row["actual_pnl"] is not None for row in selected),
                "positions": selected,
            })
    return rows


def _aggregate(values: list[float | None]) -> tuple[float | None, float | None, int]:
    present = [value for value in values if value is not None]
    if not present:
        return None, None, 0
    return statistics.fmean(present), statistics.median(present), len(present)


def build_report(root: Path) -> dict:
    filtering_directory = root / "data" / "filtering"
    minute_directory = root / "data" / "minute_bars_parquet"
    daily_directory = root / "data" / "cache" / "yahoo_daily"
    pairs = set()
    filter_days = set()
    for path in sorted(filtering_directory.glob("????-??-??.json")):
        data = _load_json(path, {})
        day = data.get("date", path.stem)
        filter_days.add(day)
        pairs.update((day, str(symbol)) for symbol in data.get("symbols", []))

    observations = []
    missing_pairs = []
    seen_minute_days = set()
    for day, symbol in sorted(pairs):
        path = _partition_path(minute_directory, day, symbol)
        if not path.exists():
            missing_pairs.append((day, symbol))
            continue
        bars = _load_bars(path)
        if not bars:
            missing_pairs.append((day, symbol))
            continue
        seen_minute_days.add(day)
        daily = _load_daily_cache(daily_directory, symbol, day)
        atr, previous_close = atr_from_daily_cache(daily, day)
        reference = atr if atr and atr > 0 else previous_close
        reference_kind = "日足ATR" if atr and atr > 0 else "前日終値" if previous_close else "なし"
        window_data = analyze_day_windows(bars, reference)
        observations.append({"day": day, "symbol": symbol, "reference": reference,
                             "reference_kind": reference_kind, "windows": window_data,
                             "first_bar": bars[0]["time"].isoformat(), "last_bar": bars[-1]["time"].isoformat()})

    time_summary = []
    for label, _, _ in TIME_WINDOWS:
        items = [item["windows"][label] for item in observations if label in item["windows"]]
        range_mean, range_median, range_count = _aggregate([item["range_30m_ratio"] for item in items])
        volume_mean, volume_median, volume_count = _aggregate([item["volume_share"] for item in items])
        flat_mean, flat_median, flat_count = _aggregate([item["unchanged_ratio"] for item in items])
        time_summary.append({"window": label, "symbol_days": len(items),
                             "range_mean": range_mean, "range_median": range_median, "range_samples": range_count,
                             "volume_mean": volume_mean, "volume_median": volume_median, "volume_samples": volume_count,
                             "flat_mean": flat_mean, "flat_median": flat_median, "flat_samples": flat_count,
                             "observed_bars": sum(item["bar_count"] for item in items),
                             "flat_pairs": sum(item["unchanged_pair_count"] for item in items)})

    order_history = _load_json(root / "data" / "trading" / "order_history.json", [])
    production_buys = _trade_positions(order_history, "production")
    backtest_data = _load_json(root / "data" / "backtest" / "latest" / "latest_timeseries.json", {})
    backtest_events = backtest_data.get("signals", []) if isinstance(backtest_data, dict) else []
    backtest_buys = _trade_positions(backtest_events, "backtest")
    cohorts = {}
    for name, buys in (("ペーパー注文履歴", production_buys), ("最新バックテスト", backtest_buys)):
        evaluated, diagnostics = evaluate_cohort(buys, minute_directory, daily_directory, buys)
        cohorts[name] = {"diagnostics": diagnostics, "summary": summarize_stagnation(evaluated),
                         "evaluated": evaluated}

    return {
        "metadata": {"filter_files": len(filter_days), "filter_days": sorted(filter_days),
                     "filter_symbol_days": len(pairs), "minute_symbol_days": len(observations),
                     "minute_days_for_filter": len(seen_minute_days), "missing_filter_symbol_days": missing_pairs,
                     "minute_partition_count": len(list(minute_directory.glob("symbol=*/date=*/data.parquet"))),
                     "minute_partition_days": len({path.parent.name[5:] for path in minute_directory.glob("symbol=*/date=*/data.parquet")}),
                     "volume_missing_bar_count": sum(
                         window["bar_count"] - window["volume_bar_count"]
                         for observation in observations for window in observation["windows"].values()
                     ),
                     "first_minute_time": min((item["first_bar"][11:16] for item in observations), default=None),
                     "last_minute_time": max((item["last_bar"][11:16] for item in observations), default=None),
                     "atr_reference_count": sum(item["reference_kind"] == "日足ATR" for item in observations),
                     "previous_close_reference_count": sum(item["reference_kind"] == "前日終値" for item in observations),
                     "no_reference_count": sum(item["reference_kind"] == "なし" for item in observations)},
        "time_summary": time_summary, "observations": observations,
        "cohorts": cohorts,
    }


def _fmt(value, percent=False):
    if value is None:
        return "判定不可"
    return f"{value * 100:.2f}%" if percent else f"{value:.4f}"


def format_markdown(report: dict, command: str, json_path: Path) -> str:
    metadata = report["metadata"]
    lines = ["# 時間帯別値動き・停滞ポジション診断", "",
             "## データ量と所見", "",
             f"- フィルタ通過: {metadata['filter_files']}日、{metadata['filter_symbol_days']}銘柄日。分足あり {metadata['minute_symbol_days']}銘柄日 / {metadata['minute_days_for_filter']}日、分足なし {len(metadata['missing_filter_symbol_days'])}銘柄日。",
             f"- 保存済み分足全体: {metadata['minute_partition_count']}銘柄日 / {metadata['minute_partition_days']}日。対象分足の記録時刻は {metadata['first_minute_time']}〜{metadata['last_minute_time']}。",
             f"- 分析Aの基準値: 日足ATR {metadata['atr_reference_count']}銘柄日、前日終値 {metadata['previous_close_reference_count']}銘柄日、どちらもなし {metadata['no_reference_count']}銘柄日。出来高なしの対象分足は {metadata['volume_missing_bar_count']}本。",
             f"- ペーパー注文履歴の約定BUY: {report['cohorts']['ペーパー注文履歴']['diagnostics']['buy_count']}件。分足あり {report['cohorts']['ペーパー注文履歴']['diagnostics']['with_minute_bars']}件。",
             f"- 最新バックテストBUY: {report['cohorts']['最新バックテスト']['diagnostics']['buy_count']}件。分足あり {report['cohorts']['最新バックテスト']['diagnostics']['with_minute_bars']}件。",
             "- サンプルは時間帯分析が銘柄日単位、停滞分析はBUY件数単位で少なく、傾向の把握まで。結論は出せない。", "",
             "## 分析A: 時間帯別", "",
             "30分換算値幅は分足終値の時間帯内レンジをATR（不足時は前日終値）で割った比率。60分帯は前後半それぞれの30分レンジの平均、10分/20分帯は観測レンジを時間に比例して30分換算した。出来高シェア・横ばい率は銘柄日ごとに算出後、平均/中央値を集計。", "",
             "| 時間帯 | 銘柄日数 | 値幅/基準 平均・中央値 | 出来高シェア 平均・中央値 | 横ばい率 平均・中央値 | 横ばい判定ペア数 |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in report["time_summary"]:
        lines.append(f"| {row['window']} | {row['symbol_days']} | {_fmt(row['range_mean'], True)} / {_fmt(row['range_median'], True)} (n={row['range_samples']}) | {_fmt(row['volume_mean'], True)} / {_fmt(row['volume_median'], True)} (n={row['volume_samples']}) | {_fmt(row['flat_mean'], True)} / {_fmt(row['flat_median'], True)} (n={row['flat_samples']}) | {row['flat_pairs']} |")
    lines.extend(["", "## 分析B: 買った後に動かなかったポジション", "",
                  "停滞判定は買値と買い時刻以降の分足終値レンジが基準×X未満。ATRは注文記録ATRを優先し、なければ保存済み日足ATR、どちらもなければ前日終値×0.5%を基準とした。N時点はN分経過後の最初の分足終値で判定し、15:20を上限とした。仮想損益差は「仮想損益 - 実際損益」。", ""])
    for cohort_name, cohort in report["cohorts"].items():
        diagnostics = cohort["diagnostics"]
        lines.extend([f"### {cohort_name}", "",
                      f"BUY {diagnostics['buy_count']}件、分足あり {diagnostics['with_minute_bars']}件、ATR基準 {diagnostics['atr_reference_count']}件、前日終値0.5%基準 {diagnostics['previous_close_fallback_count']}件、実損益を確定できないBUY {diagnostics['actual_pnl_unavailable_count']}件。", "",
                      "| N | X | 該当数 | 仮想損益 合計 (spreadなし / 0.1%) | 実際との差 合計 (spreadなし / 0.1%) | 差を計算できた数 |",
                      "|---:|---:|---:|---:|---:|---:|"])
        for row in cohort["summary"]:
            lines.append(f"| {row['elapsed_minutes']}分 | {row['atr_fraction']:.0%} | {row['count']} | {row['virtual_pnl_no_spread']:.2f} / {row['virtual_pnl_spread']:.2f} | {row['actual_difference_no_spread']:.2f} / {row['actual_difference_spread']:.2f} | {row['actual_pnl_count']} |")
        details = [row for row in cohort["evaluated"] if any(
            row in summary["positions"] for summary in cohort["summary"]
        )]
        lines.extend(["", "停滞した各判定の補足（最大上昇/下落は判定時売値から15:20までの分足終値差、候補は同日後刻の実約定BUYのみ）:", "",
                      "| 銘柄・日 | N | 判定時刻 | 基準 | レンジ | 最大上昇 / 下落 | 後刻の他銘柄BUY |", "|---|---:|---|---:|---:|---:|---|"])
        seen = set()
        for row in details:
            fractions = [summary["atr_fraction"] for summary in cohort["summary"] if row in summary["positions"]]
            key = (row["symbol"], row["day"], row["elapsed_minutes"])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"| {row['symbol']} {row['day']} | {row['elapsed_minutes']}分 (X={','.join(f'{value:.0%}' for value in fractions)}) | {row['evaluation_time'][11:19]} | {row['reference']:.4f} ({row['reference_kind']}) | {row['observed_range']:.4f} | {row['max_rise_after'] if row['max_rise_after'] is not None else 'なし'} / {row['max_fall_after'] if row['max_fall_after'] is not None else 'なし'} | {','.join(row['other_executed_buys_after']) or 'なし'} |")
        if diagnostics["without_minute_bars"] or diagnostics["without_reference"]:
            lines.extend(["", f"分足なし: {', '.join(diagnostics['without_minute_bars']) or 'なし'}。基準値なし: {', '.join(diagnostics['without_reference']) or 'なし'}。"])
        lines.append("")
    lines.extend(["## 制約・分からなかったこと", "",
                  f"- フィルタ通過銘柄日の分足欠損は {len(metadata['missing_filter_symbol_days'])}件。個別一覧はJSONに記録。ATRを作れない銘柄日は前日終値で代替し、前日終値もない {metadata['no_reference_count']}銘柄日は値幅比を判定しない。",
                  "- 保存分足は価格/出来高のみで分足OHLCがないため、値幅とその後の最大上昇・下落は分足終値レンジによる下限近似。場中の高値/安値は復元できない。",
                  "- 他候補のエントリー条件は候補ごとの時刻別シグナル記録がないため網羅判定できない。表には手放し時刻以降に実際に約定した他銘柄BUYだけを参考表示しており、未約定候補の有無は分からない。",
                  "- 欠損分足、出来高が保存されていない分、日足キャッシュ欠損、ゼロ価格SELLや未決済で実損益を確定できないBUYは診断JSONで確認すること。バックテストは `data/backtest/latest/latest_timeseries.json` の最新スナップショットのみ。", "",
                  "## 再実行", "", f"`{command}`", "", f"JSON詳細: `{json_path.as_posix()}`", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path,
                        default=Path("data/backtest_v2_scratch/diagnostics/intraday_movement_and_stagnation_report.md"))
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    json_path = output.with_suffix(".json")
    report = build_report(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    command = ".\\.venv\\Scripts\\python.exe scripts/analysis/analyze_intraday_movement_and_stagnant_positions.py"
    output.write_text(format_markdown(report, command, json_path.relative_to(root)), encoding="utf-8")
    print(f"Report: {output}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())