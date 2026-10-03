"""日次レポートの損益項目を注文履歴から再計算し、該当する週次・月次レポートを再生成する。

9/28当時の日次レポートはtotal_profit_lossを保有の評価損益だけで出しており、
強制決済後の実現損益が0になっていた(実現損益の反映はコミット3349b7a)。
このスクリプトは損益3項目だけを書き換える。Slack通知・LLM分析・外部API・DB書き込みは行わない。

実行方法(既定はドライラン):
    python scripts/ops/recompute_daily_reports.py
    python scripts/ops/recompute_daily_reports.py --apply
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config  # noqa: E402
from src.infrastructure.paper.paper_order_client import PaperOrderClient  # noqa: E402

REPORT_ROOT = PROJECT_ROOT / "data" / "reports"
ORDER_HISTORY_PATH = PROJECT_ROOT / "data" / "trading" / "order_history.json"
STATE_PATH = PROJECT_ROOT / "data" / "trading" / "paper_account_state.json"
DEFAULT_TARGET_DATES = ("2026-09-14", "2026-09-18", "2026-09-28")
DEFAULT_CHECK_DATES = ("2026-09-25", "2026-10-02")
RECOMPUTE_SOURCE = "order_history.json replayed through PaperOrderClient (scripts/ops/recompute_daily_reports.py)"
TOLERANCE = 0.005

# 前回の調査結果(ドライランの突合用)
EXPECTED_DAILY = {"2026-09-14": -4.83, "2026-09-18": -50.40, "2026-09-28": 362.77}
EXPECTED_PERIOD = {
    "weekly/2026-09-14_2026-09-18.json": -55.23,
    "weekly/2026-09-28_2026-10-02.json": 289.48,
    "monthly/2026-09.json": 307.54,
}
EXPECTED_CASH_GAIN = 234.245


@dataclass
class ReplayResult:
    daily_realized: "OrderedDict[str, float]"
    order_counts: dict[str, int]
    final_cash: float
    holdings: dict[str, int]
    warnings: list[str] = field(default_factory=list)


def load_order_history(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"注文履歴の形式が不正です: {path}")
    return data


def replay_order_history(
    history: list[dict],
    initial_cash: float,
    fee_rate: float | None = None,
    market_slippage_bps: float | None = None,
) -> ReplayResult:
    """注文履歴を日ごとにPaperOrderClientへ再生し、日次の実現損益を返す。

    履歴のpriceは発注前の板価格(TradeSignal.price)なので、本番と同じく価格が正のときだけ
    set_priceで反映する。0.0のときはクライアント内に残っている直近価格で約定する。
    """
    if not history:
        return ReplayResult(OrderedDict(), {}, initial_cash, {})
    current_day = [date.fromisoformat(history[0]["timestamp"][:10])]
    kwargs = {}
    if fee_rate is not None:
        kwargs["fee_rate"] = fee_rate
    if market_slippage_bps is not None:
        kwargs["market_slippage_bps"] = market_slippage_bps
    client = PaperOrderClient(
        prices={},
        cash=initial_cash,
        state_path=None,
        realized_pnl_date=current_day[0].isoformat(),
        today_provider=lambda: current_day[0],
        **kwargs,
    )
    daily_realized: "OrderedDict[str, float]" = OrderedDict()
    order_counts: dict[str, int] = {}
    warnings: list[str] = []
    for entry in history:
        day_text = entry["timestamp"][:10]
        current_day[0] = date.fromisoformat(day_text)
        order_counts[day_text] = order_counts.get(day_text, 0) + 1
        price = float(entry["price"] or 0)
        symbol = entry["symbol"]
        if price > 0:
            client.set_price(symbol, price)
        elif symbol not in client.prices:
            warnings.append(f"{day_text} {symbol}: 価格が0で、参照できる直近価格もないため約定を再現できません")
            continue
        else:
            warnings.append(
                f"{day_text} {symbol} side={entry['side']}: 履歴の価格が0のため、直近価格{client.prices[symbol]}で再生しました"
            )
        result = client.place_market_order("recompute", symbol, str(entry["side"]), int(entry["qty"]))
        if result is None:
            warnings.append(f"{day_text} {symbol} side={entry['side']}: 再生時に約定しませんでした")
        daily_realized[day_text] = client.get_daily_realized_pnl()
    return ReplayResult(daily_realized, order_counts, client.cash, dict(client.holdings), warnings)


def existing_unrealized(report: dict) -> float:
    """現行コードの定義: last_positionsのProfitLoss合計。レポートに残っている値をそのまま使う。"""
    if report.get("unrealized_profit_loss") is not None:
        return float(report["unrealized_profit_loss"])
    return sum(float(position.get("profit_loss", 0) or 0) for position in report.get("positions") or [])


def recomputed_report(report: dict, realized: float, recomputed_at: str) -> dict:
    """損益3項目と再作成メタデータだけを差し替えた日次レポートを返す。"""
    unrealized = existing_unrealized(report)
    total = realized + unrealized
    original_total = report.get("original_total_profit_loss", report.get("total_profit_loss"))
    result: dict = {}
    for key, value in report.items():
        if key in ("realized_profit_loss", "unrealized_profit_loss"):
            continue
        if key == "total_profit_loss":
            result["realized_profit_loss"] = realized
            result["unrealized_profit_loss"] = unrealized
            result["total_profit_loss"] = total
            continue
        result[key] = value
    result["original_total_profit_loss"] = original_total
    result["recomputed"] = True
    result["recomputed_at"] = recomputed_at
    result["recompute_source"] = RECOMPUTE_SOURCE
    return result


def _differences(old, new, path="") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        paths: list[str] = []
        for key in sorted(set(old) | set(new)):
            child = f"{path}.{key}" if path else str(key)
            if key not in old or key not in new:
                paths.append(child)
            else:
                paths.extend(_differences(old[key], new[key], child))
        return paths
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return [path]
        paths = []
        for index, (left, right) in enumerate(zip(old, new)):
            paths.extend(_differences(left, right, f"{path}[{index}]"))
        return paths
    return [] if old == new else [path]


def _is_profit_path(path: str) -> bool:
    return path.rsplit(".", 1)[-1] == "total_profit_loss"


def patch_period_report(
    original: dict, new_totals: dict[str, float], old_totals: dict[str, float]
) -> tuple[dict, list[str]]:
    """週次・月次レポートのtotal_profit_loss(日別・合計・比較)だけを差し替えて返す。"""
    updated = copy.deepcopy(original)
    problems: list[str] = []
    daily = updated["daily"]
    reports = daily.get("reports") or []

    def total_of(rows: list[dict]) -> float:
        return round(sum(float(row.get("total_profit_loss") or 0) for row in rows), 2)

    if abs(total_of(reports) - float(daily.get("total_profit_loss") or 0)) > TOLERANCE:
        problems.append("元のdaily.total_profit_lossが日別の合計と一致しません")
    start, end = original["period"]["start"], original["period"]["end"]
    for day, total in new_totals.items():
        if not start <= day <= end:
            continue
        row = next((item for item in reports if str(item.get("date"))[:10] == day), None)
        if row is None:
            problems.append(f"{day}がdaily.reportsにありません")
            continue
        row["total_profit_loss"] = total
    daily["total_profit_loss"] = total_of(reports)

    comparison = updated.get("comparison")
    if isinstance(comparison, dict):
        current = comparison.get("current")
        if isinstance(current, dict):
            current["total_profit_loss"] = daily["total_profit_loss"]
        previous = comparison.get("previous")
        if isinstance(previous, dict) and isinstance(previous.get("period"), dict):
            previous_start, previous_end = previous["period"]["start"], previous["period"]["end"]
            delta = sum(
                total - old_totals.get(day, 0.0)
                for day, total in new_totals.items()
                if previous_start <= day <= previous_end
            )
            if abs(delta) > TOLERANCE:
                previous_total = float(previous.get("total_profit_loss") or 0)
                previous["total_profit_loss"] = round(previous_total + delta, 2)
    return updated, problems


@dataclass
class FilePlan:
    relative_path: str
    original: dict
    updated: dict
    changed_paths: list[str]
    unexpected_paths: list[str]
    will_write: bool
    note: str = ""


@dataclass
class Plan:
    replay: ReplayResult
    daily_rows: list[dict]
    daily_files: list[FilePlan]
    period_files: list[FilePlan]
    mismatches: list[str]
    cash_gain: float | None
    state_cash_gain: float | None
    order_count_notes: list[str] = field(default_factory=list)
    consistency_notes: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _period_contains(period: dict, dates: set[str]) -> bool:
    return any(period["start"] <= item <= period["end"] for item in dates)


def build_plan(
    report_root: Path,
    history: list[dict],
    state: dict | None,
    target_dates: tuple[str, ...],
    check_dates: tuple[str, ...],
    recomputed_at: str,
    initial_cash: float,
    fee_rate: float | None = None,
    market_slippage_bps: float | None = None,
) -> Plan:
    replay = replay_order_history(history, initial_cash, fee_rate, market_slippage_bps)
    daily_directory = report_root / "daily"
    targets = set(target_dates)

    daily_rows: list[dict] = []
    daily_files: list[FilePlan] = []
    staged: dict[str, dict] = {}
    for day in sorted(set(target_dates) | set(check_dates)):
        path = daily_directory / f"{day}.json"
        if not path.exists():
            daily_rows.append({"date": day, "current": None, "recomputed": replay.daily_realized.get(day), "note": "日次レポートなし"})
            continue
        report = _read_json(path)
        realized = replay.daily_realized.get(day, 0.0)
        updated = recomputed_report(report, realized, recomputed_at)
        current_total = float(report.get("total_profit_loss") or 0)
        new_total = updated["total_profit_loss"]
        differs = abs(current_total - new_total) > TOLERANCE
        is_target = day in targets
        will_write = is_target and differs
        if is_target and not differs:
            note = "対象日だが差なし(変更しない)"
        elif is_target:
            note = "更新対象"
        elif differs:
            note = "確認日: 差あり(変更しない)"
        else:
            note = "確認日: 差なし(変更しない)"
        daily_rows.append({"date": day, "current": current_total, "recomputed": new_total, "note": note})
        daily_files.append(FilePlan(
            f"daily/{day}.json", report, updated,
            _differences(report, updated), [], will_write, note,
        ))
        if will_write:
            staged[day] = updated

    # 週次・月次は日次のtotal_profit_lossの単純合計なので、保存済みの値のうち損益項目だけを差し替える。
    # 再生成すると、週次作成後に変わった他の日次レポートの内容まで取り込んでしまうため。
    new_totals = {day: report["total_profit_loss"] for day, report in staged.items()}
    old_totals = {
        item.relative_path[len("daily/"):-len(".json")]: float(item.original.get("total_profit_loss") or 0)
        for item in daily_files
    }
    period_files: list[FilePlan] = []
    candidates: list[dict] = []
    for kind in ("weekly", "monthly"):
        for path in sorted((report_root / kind).glob("*.json")):
            original = _read_json(path)
            if "period" not in original or "daily" not in original:
                period_files.append(FilePlan(
                    f"{kind}/{path.name}", original, original, [], [], False, "旧形式のため対象外",
                ))
                continue
            updated, problems = patch_period_report(original, new_totals, old_totals)
            candidates.append({
                "kind": kind, "name": path.name, "original": original, "updated": updated, "problems": problems,
            })

    # comparison.previousが、同じ期間の週次・月次レポートの合計と食い違っていれば揃える
    totals_by_period = {
        (item["kind"], item["updated"]["period"]["start"], item["updated"]["period"]["end"]):
            float(item["updated"]["daily"].get("total_profit_loss") or 0)
        for item in candidates
    }
    consistency_notes: list[str] = []
    for item in candidates:
        previous = (item["updated"].get("comparison") or {}).get("previous")
        if not isinstance(previous, dict) or not isinstance(previous.get("period"), dict):
            continue
        key = (item["kind"], previous["period"]["start"], previous["period"]["end"])
        expected_total = totals_by_period.get(key)
        stored = float(previous.get("total_profit_loss") or 0)
        if expected_total is None:
            consistency_notes.append(f"{item['kind']}/{item['name']}: previousの期間{key[1]}〜{key[2]}のレポートがなく確認不可(現在値 {stored})")
        elif abs(stored - expected_total) > TOLERANCE:
            previous["total_profit_loss"] = round(expected_total, 2)
            consistency_notes.append(
                f"{item['kind']}/{item['name']}: previous.total_profit_lossを {stored} から {round(expected_total, 2)} に揃えました"
            )
        else:
            consistency_notes.append(f"{item['kind']}/{item['name']}: previousは{key[1]}〜{key[2]}の合計 {stored} と一致")

    for item in candidates:
        original, updated, problems = item["original"], item["updated"], item["problems"]
        changed = _differences(original, updated)
        unexpected = [entry for entry in changed if not _is_profit_path(entry)]
        previous_period = ((original.get("comparison") or {}).get("previous") or {}).get("period")
        in_scope = _period_contains(original["period"], targets) or (
            isinstance(previous_period, dict) and _period_contains(previous_period, targets)
        )
        if problems:
            note = "整合性の問題あり(書き込まない): " + " / ".join(problems)
        elif not changed:
            note = "変更なし"
        elif in_scope:
            note = "更新対象"
        else:
            note = "対象外だが差分あり(変更しない)"
        period_files.append(FilePlan(
            f"{item['kind']}/{item['name']}", original, updated, changed, unexpected,
            bool(changed) and in_scope and not problems and not unexpected, note,
        ))
    period_files.sort(key=lambda entry: entry.relative_path)

    mismatches = []
    for day, expected in EXPECTED_DAILY.items():
        actual = replay.daily_realized.get(day)
        if actual is None or abs(actual - expected) > TOLERANCE:
            mismatches.append(f"日次 {day}: 期待値 {expected} / 再計算 {actual}")
    for relative, expected in EXPECTED_PERIOD.items():
        plan = next((item for item in period_files if item.relative_path == relative), None)
        actual = plan.updated["daily"]["total_profit_loss"] if plan else None
        if actual is None or abs(actual - expected) > TOLERANCE:
            mismatches.append(f"{relative}: 期待値 {expected} / 再作成後 {actual}")

    cash_gain = round(replay.final_cash - initial_cash, 3)
    state_cash_gain = round(float(state["cash"]) - initial_cash, 3) if state and "cash" in state else None
    return Plan(
        replay, daily_rows, daily_files, period_files, mismatches, cash_gain, state_cash_gain,
        consistency_notes=consistency_notes,
    )


def _atomic_write(path: Path, data: dict) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    os.replace(temp_path, path)


def apply_plan(plan: Plan, report_root: Path, backup_root: Path) -> list[str]:
    """バックアップを先に作成・検証してから、対象ファイルだけを上書きする。"""
    targets = [item for item in plan.daily_files + plan.period_files if item.will_write]
    for item in targets:
        backup_path = backup_root / item.relative_path
        if backup_path.exists():
            raise FileExistsError(f"バックアップが既に存在するため中止します: {backup_path}")
    for item in targets:
        source = report_root / item.relative_path
        backup_path = backup_root / item.relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_path)
        if backup_path.read_bytes() != source.read_bytes():
            raise OSError(f"バックアップの内容が一致しません: {backup_path}")
    written = []
    for item in targets:
        _atomic_write(report_root / item.relative_path, item.updated)
        written.append(item.relative_path)
    return written


def format_report(plan: Plan, apply: bool, backup_root: Path) -> str:
    lines = ["=== 日次レポート(現在 / 再計算 / 差) ==="]
    for row in plan.daily_rows:
        if row["current"] is None:
            lines.append(f"{row['date']}  {row['note']}  再計算={row['recomputed']}")
            continue
        diff = row["recomputed"] - row["current"]
        lines.append(
            f"{row['date']}  現在={row['current']:>9.2f}  再計算={row['recomputed']:>9.2f}  差={diff:>+9.2f}  {row['note']}"
        )
    lines.append("")
    lines.append("=== 週次・月次(現在 / 再作成後) ===")
    for item in plan.period_files:
        if "daily" not in item.original:
            lines.append(f"{item.relative_path}  {item.note}")
            continue
        current = item.original["daily"]["total_profit_loss"]
        rebuilt = item.updated["daily"]["total_profit_loss"]
        lines.append(f"{item.relative_path}  現在={current:>9.2f}  再作成後={rebuilt:>9.2f}  {item.note}")
        if item.changed_paths:
            lines.append("    差分: " + ", ".join(item.changed_paths))
    lines.append("")
    lines.append("=== 検算 ===")
    lines.append(f"再計算した全日の実現損益の合計: {sum(plan.replay.daily_realized.values()):.3f}")
    lines.append(f"再生後の現金 - 元手: {plan.cash_gain}")
    lines.append(f"状態ファイルの現金 - 元手: {plan.state_cash_gain}  (期待値 {EXPECTED_CASH_GAIN})")
    cash_ok = plan.state_cash_gain is not None and abs(plan.cash_gain - plan.state_cash_gain) <= 0.01
    lines.append(f"再生後の保有: {plan.replay.holdings or 'なし'}")
    lines.append("")
    lines.append("=== 期待値(前回調査)とのずれ ===")
    lines.extend(plan.mismatches or ["ずれなし"])
    lines.append("")
    lines.append("=== 再生時の注意 ===")
    lines.extend(plan.replay.warnings or ["なし"])
    lines.append("")
    lines.append("=== 別件(今回は変更しない): 日次レポートと注文履歴の注文件数の不一致 ===")
    lines.extend(plan.order_count_notes or ["なし"])
    lines.append("")
    lines.append("=== 前週・前月比較(comparison.previous)の整合確認 ===")
    lines.extend(plan.consistency_notes or ["なし"])
    writes = [item.relative_path for item in plan.daily_files + plan.period_files if item.will_write]
    lines.append(f"書き込み対象: {len(writes)}件")
    lines.extend(f"  - {item}" for item in writes)
    lines.append(f"バックアップ先: {backup_root}")
    lines.append("モード: " + ("--apply(書き込みました)" if apply else "ドライラン(何も書き込んでいません)"))
    return "\n".join(lines)


def collect_order_count_notes(report_root: Path, history_counts: dict[str, int]) -> list[str]:
    notes = []
    for path in sorted((report_root / "daily").glob("*.json")):
        report = _read_json(path)
        day = str(report.get("date", path.stem))[:10]
        report_count = int(report.get("order_count") or 0)
        history_count = history_counts.get(day, 0)
        if report_count != history_count:
            symbols = sorted({order.get("symbol") for order in report.get("orders") or []})
            notes.append(f"{day}: 日次レポート {report_count}件 / 注文履歴 {history_count}件 (レポート内の銘柄: {symbols})")
    return notes


def run(
    apply: bool,
    report_root: Path = REPORT_ROOT,
    order_history_path: Path = ORDER_HISTORY_PATH,
    state_path: Path = STATE_PATH,
    backup_root: Path | None = None,
    target_dates: tuple[str, ...] = DEFAULT_TARGET_DATES,
    check_dates: tuple[str, ...] = DEFAULT_CHECK_DATES,
    now: Callable[[], datetime] = datetime.now,
    initial_cash: float | None = None,
    output: Callable[[str], None] = print,
) -> Plan:
    current = now()
    backup_root = backup_root or report_root / f"_backup_{current:%Y%m%d}"
    history = load_order_history(order_history_path)
    state = _read_json(state_path) if state_path.exists() else None
    plan = build_plan(
        report_root, history, state, target_dates, check_dates,
        current.isoformat(timespec="seconds"),
        config.OPERATING_CAPITAL if initial_cash is None else initial_cash,
    )
    plan.order_count_notes = collect_order_count_notes(report_root, plan.replay.order_counts)
    if apply:
        apply_plan(plan, report_root, backup_root)
    output(format_report(plan, apply, backup_root))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="日次レポートの損益を注文履歴から再計算し、週次・月次を再作成します")
    parser.add_argument("--apply", action="store_true", help="指定時のみ書き込む(既定はドライラン)")
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--target-dates", nargs="+", default=list(DEFAULT_TARGET_DATES))
    parser.add_argument("--check-dates", nargs="+", default=list(DEFAULT_CHECK_DATES))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run(
        args.apply,
        report_root=args.report_root,
        backup_root=args.backup_root,
        target_dates=tuple(args.target_dates),
        check_dates=tuple(args.check_dates),
    )


if __name__ == "__main__":
    main()
