"""テストの出力が混入した日次レポートに「分析に使わない」印(data_qualityフィールド)を付ける。

内容(注文・損益など)の書き換えや削除はしない。追加するのは data_quality / data_quality_reason /
data_quality_marked_at の3項目だけ。Slack通知・LLM分析・外部API・DB書き込みは行わない。

実行方法(既定はドライラン):
    python scripts/ops/mark_report_data_quality.py
    python scripts/ops/mark_report_data_quality.py --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_ROOT = PROJECT_ROOT / "data" / "reports"
BACKUP_NAME = "_backup_20261003_quality"
ADDED_FIELDS = ("data_quality", "data_quality_reason", "data_quality_marked_at")


@dataclass(frozen=True)
class Target:
    date: str
    quality: str
    reason: str
    # 調査時に確認した内容と同じファイルにだけ印を付けるための、調査時点のSHA-256
    expected_sha256: str


TARGETS = (
    Target(
        "2026-09-04", "test_contaminated",
        "一時コピーで実行したtests/test_trading_bot.pyの出力とSHA-256が一致(2026-10-03調査)",
        "B7F559844FA86A16EBC392413D081079925591D85EAD3E8F191F7BEBF6B04906",
    ),
    Target(
        "2026-09-16", "test_contaminated",
        "一時コピーで実行したtests/test_trading_bot.pyの出力とSHA-256が一致(2026-10-03調査)。注文履歴に7203の注文なし",
        "739A9848A608E364FD6E2CC101E439B894859E3C1B4F24752654F563B8643F5D",
    ),
    Target(
        "2026-09-17", "suspected_test_contaminated",
        "7203を2,200円で200株の注文と保有銘柄(7203/1301/1332/1605/1801)がtests/test_trading_bot.pyのフィクスチャと一致。当時のファイルは残っておらずSHA-256照合は不可。注文履歴に7203の注文なし",
        "C3974F0B044C513C0DA23E2F2346A929EF2C53907521F37B5B4AB33A7EACBF1B",
    ),
)


@dataclass
class TargetPlan:
    target: Target
    relative_path: str
    status: str  # mark / already_marked / skip
    note: str
    original_text: str | None = None
    updated: dict | None = None
    updated_text: str | None = None
    current_quality: str | None = None
    order_count: int | None = None
    will_write: bool = False


@dataclass
class PeriodUsage:
    relative_path: str
    marked_days: list[dict]
    totals: dict
    marked_totals: dict
    summary_changed_by_marking: bool = False


@dataclass
class Plan:
    targets: list[TargetPlan]
    usages: list[PeriodUsage] = field(default_factory=list)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _dump(data: dict, newline: str = "\n") -> str:
    # write_jsonと同じ整形。末尾の改行は付けず、既存ファイルの改行コードに合わせる。
    return json.dumps(data, ensure_ascii=False, indent=4).replace("\n", newline)


def plan_target(report_root: Path, target: Target, marked_at: str) -> TargetPlan:
    relative = f"daily/{target.date}.json"
    path = report_root / relative
    if not path.exists():
        return TargetPlan(target, relative, "skip", "日次レポートなし")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    data = json.loads(text)
    current = data.get("data_quality")
    order_count = data.get("order_count")
    if current is not None:
        status = "already_marked" if current == target.quality else "skip"
        note = "既に印あり(変更しない)" if status == "already_marked" else f"別の印あり: {current}"
        return TargetPlan(target, relative, status, note, current_quality=current, order_count=order_count)
    if hashlib.sha256(raw).hexdigest().upper() != target.expected_sha256:
        return TargetPlan(
            target, relative, "skip",
            "調査時点と内容が変わっているため中止(SHA-256不一致)", order_count=order_count,
        )
    newline = "\r\n" if "\r\n" in text else "\n"
    if _dump(data, newline) != text:
        return TargetPlan(
            target, relative, "skip", "再整形すると追加項目以外にも差分が出るため中止", order_count=order_count,
        )
    updated = dict(data)
    updated["data_quality"] = target.quality
    updated["data_quality_reason"] = target.reason
    updated["data_quality_marked_at"] = marked_at
    return TargetPlan(
        target, relative, "mark", f"印を付ける: {target.quality}",
        original_text=text, updated=updated, updated_text=_dump(updated, newline),
        order_count=order_count, will_write=True,
    )


def summarize_period_usage(report_root: Path, marked_dates: set[str]) -> list[PeriodUsage]:
    """週次・月次(読み取りのみ)が、印を付けた日をどの項目にどれだけ含んでいるかを集計する。"""
    usages = []
    for kind in ("weekly", "monthly"):
        for path in sorted((report_root / kind).glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            daily = data.get("daily")
            if not isinstance(daily, dict) or not isinstance(daily.get("reports"), list):
                continue
            rows = [row for row in daily["reports"] if str(row.get("date"))[:10] in marked_dates]
            if not rows:
                continue
            statuses: dict[str, int] = {}
            for row in rows:
                status = str(row.get("market_assessment_status") or "not_recorded")
                statuses[status] = statuses.get(status, 0) + 1
            marked_totals = {
                "report_count": len(rows),
                "order_count": sum(int(row.get("order_count") or 0) for row in rows),
                "total_profit_loss": round(sum(float(row.get("total_profit_loss") or 0) for row in rows), 2),
                "log_error_count": sum(int(row.get("log_error_count") or 0) for row in rows),
                "kill_switch_days": sum(1 for row in rows if row.get("kill_switch_triggered")),
                "emergency_stop_days": sum(1 for row in rows if row.get("emergency_stop_triggered")),
                "market_assessment_status_counts": statuses,
            }
            operational = daily.get("operational_summary") or {}
            totals = {
                "report_count": daily.get("report_count"),
                "order_count": daily.get("order_count"),
                "total_profit_loss": daily.get("total_profit_loss"),
                "log_error_count": operational.get("log_error_count"),
                "kill_switch_days": daily.get("kill_switch_days"),
                "emergency_stop_days": daily.get("emergency_stop_days"),
                "market_assessment_status_counts": operational.get("market_assessment_status_counts"),
            }
            usages.append(PeriodUsage(
                f"{kind}/{path.name}",
                [{"date": str(row.get("date"))[:10], "order_count": row.get("order_count"),
                  "total_profit_loss": row.get("total_profit_loss"),
                  "market_assessment_status": row.get("market_assessment_status")} for row in rows],
                totals, marked_totals,
            ))
    return usages


def build_plan(report_root: Path, marked_at: str, targets: tuple[Target, ...] = TARGETS) -> Plan:
    plans = [plan_target(report_root, target, marked_at) for target in targets]
    return Plan(plans, summarize_period_usage(report_root, {target.date for target in targets}))


def apply_plan(plan: Plan, report_root: Path, backup_root: Path) -> list[str]:
    """バックアップを先に作成・ハッシュ検証してから、一時ファイル経由で置き換える。"""
    writes = [item for item in plan.targets if item.will_write]
    for item in writes:
        backup_path = backup_root / item.relative_path
        if backup_path.exists():
            raise FileExistsError(f"バックアップが既に存在するため中止します: {backup_path}")
    for item in writes:
        source = report_root / item.relative_path
        backup_path = backup_root / item.relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_path)
        if _file_sha256(backup_path) != _file_sha256(source):
            raise OSError(f"バックアップのハッシュが一致しません: {backup_path}")
    written = []
    for item in writes:
        path = report_root / item.relative_path
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_bytes(item.updated_text.encode("utf-8"))
        os.replace(temp_path, path)
        verify_written(path, item)
        written.append(item.relative_path)
    return written


def verify_written(path: Path, item: TargetPlan) -> None:
    after = json.loads(path.read_text(encoding="utf-8"))
    before = json.loads(item.original_text)
    extra = {key: value for key, value in after.items() if key not in before}
    if set(extra) != set(ADDED_FIELDS) or {key: after[key] for key in before} != before:
        raise AssertionError(f"追加項目以外が変わっています: {path}")


def format_report(plan: Plan, apply: bool, backup_root: Path) -> str:
    lines = ["=== 対象日(現在の状態 / 付ける印) ==="]
    for item in plan.targets:
        lines.append(
            f"{item.target.date}  注文件数={item.order_count}  現在の印={item.current_quality or 'なし'}"
            f"  付ける印={item.target.quality}  結果: {item.note}"
        )
    lines.append("")
    lines.append("=== 週次・月次への含まれ方(読み取りのみ。集計からの除外は行わない) ===")
    for usage in plan.usages:
        lines.append(usage.relative_path)
        for key, value in usage.marked_totals.items():
            lines.append(f"    {key}: 印を付けた日の分={value} / 集計全体={usage.totals.get(key)}")
        for row in usage.marked_days:
            lines.append(f"    - {row['date']}: 注文{row['order_count']}件, 損益{row['total_profit_loss']}, 市場判定={row['market_assessment_status']}")
    lines.append("")
    writes = [item.relative_path for item in plan.targets if item.will_write]
    lines.append(f"書き込み対象: {len(writes)}件")
    lines.extend(f"  - {item}" for item in writes)
    lines.append(f"バックアップ先: {backup_root}")
    lines.append("モード: " + ("--apply(書き込みました)" if apply else "ドライラン(何も書き込んでいません)"))
    return "\n".join(lines)


def run(
    apply: bool,
    report_root: Path = REPORT_ROOT,
    backup_root: Path | None = None,
    now: Callable[[], datetime] = datetime.now,
    targets: tuple[Target, ...] = TARGETS,
    output: Callable[[str], None] = print,
) -> Plan:
    backup_root = backup_root or report_root / BACKUP_NAME
    plan = build_plan(report_root, now().isoformat(timespec="seconds"), targets)
    if apply:
        apply_plan(plan, report_root, backup_root)
    output(format_report(plan, apply, backup_root))
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="テスト混入が疑われる日次レポートに分析除外の印を付けます")
    parser.add_argument("--apply", action="store_true", help="指定時のみ書き込む(既定はドライラン)")
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    parser.add_argument("--backup-root", type=Path, default=None)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run(args.apply, report_root=args.report_root, backup_root=args.backup_root)


if __name__ == "__main__":
    main()
