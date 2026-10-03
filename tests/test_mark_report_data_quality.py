import hashlib
import json
from datetime import date, datetime

import pytest

from scripts.ops import mark_report_data_quality as mark
from src.infrastructure.analysis.summary_loader import load_daily_summaries, summarize_daily_reports

NOW = datetime(2026, 10, 3, 22, 30, 0)


def _write_report(path, data, newline="\r\n"):
    text = json.dumps(data, ensure_ascii=False, indent=4).replace("\n", newline)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _report(day, order_count=0):
    return {
        "date": day, "trading_mode": "ペーパートレード", "order_count": order_count,
        "orders": [{"symbol": "7203", "price": 92.0}] * order_count, "positions": [],
        "total_profit_loss": 0.0, "kill_switch_triggered": False, "emergency_stop_triggered": False,
        "market_conditions": {"assessment_status": "not_evaluated"}, "report_text": "本文",
    }


def _workspace(tmp_path, newline="\r\n"):
    root = tmp_path / "reports"
    hashes = {}
    for day, count in (("2026-09-04", 0), ("2026-09-16", 1), ("2026-09-18", 2)):
        hashes[day] = _write_report(root / "daily" / f"{day}.json", _report(day, count), newline)
    targets = (
        mark.Target("2026-09-04", "test_contaminated", "理由A", hashes["2026-09-04"]),
        mark.Target("2026-09-16", "suspected_test_contaminated", "理由B", hashes["2026-09-16"]),
    )
    weekly = {
        "week_start": "2026-09-14", "week_end": "2026-09-18",
        "daily": {
            "report_count": 2, "order_count": 3, "total_profit_loss": 0.0, "kill_switch_days": 0,
            "emergency_stop_days": 0,
            "operational_summary": {"log_error_count": 0, "market_assessment_status_counts": {"not_evaluated": 2}},
            "reports": [
                {"date": "2026-09-16", "order_count": 1, "total_profit_loss": 0.0,
                 "market_assessment_status": "not_evaluated", "log_error_count": 0},
                {"date": "2026-09-18", "order_count": 2, "total_profit_loss": 0.0,
                 "market_assessment_status": "not_evaluated", "log_error_count": 0},
            ],
        },
    }
    weekly_path = root / "weekly" / "2026-09-14_2026-09-18.json"
    weekly_path.parent.mkdir(parents=True)
    weekly_path.write_text(json.dumps(weekly, ensure_ascii=False), encoding="utf-8")
    return root, targets


def _snapshot(root):
    return {path: path.read_bytes() for path in sorted(root.rglob("*.json"))}


def test_dry_run_writes_nothing(tmp_path):
    root, targets = _workspace(tmp_path)
    before = _snapshot(root)
    messages = []

    plan = mark.run(False, report_root=root, now=lambda: NOW, targets=targets, output=messages.append)

    assert _snapshot(root) == before
    assert not (root / mark.BACKUP_NAME).exists()
    assert not list(root.rglob("*.tmp"))
    assert [item.status for item in plan.targets] == ["mark", "mark"]
    assert "ドライラン" in messages[0]


def test_apply_backs_up_first_and_adds_only_the_three_fields(tmp_path, monkeypatch):
    root, targets = _workspace(tmp_path)
    before = _snapshot(root)
    backup_root = root / mark.BACKUP_NAME
    seen = []
    real_replace = mark.os.replace

    def checking_replace(source, destination):
        relative = destination.relative_to(root)
        seen.append((backup_root / relative).read_bytes() == destination.read_bytes())
        real_replace(source, destination)

    monkeypatch.setattr(mark.os, "replace", checking_replace)

    mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)

    assert seen == [True, True]
    for day in ("2026-09-04", "2026-09-16"):
        path = root / "daily" / f"{day}.json"
        original_text = before[path].decode("utf-8")
        assert (backup_root / "daily" / f"{day}.json").read_bytes() == before[path]
        updated = json.loads(path.read_text(encoding="utf-8"))
        original = json.loads(original_text)
        assert {key: updated[key] for key in original} == original
        assert set(updated) - set(original) == set(mark.ADDED_FIELDS)
        assert updated["data_quality_marked_at"] == "2026-10-03T22:30:00"
        assert b"\r\n" in path.read_bytes()
    first = json.loads((root / "daily" / "2026-09-04.json").read_text(encoding="utf-8"))
    second = json.loads((root / "daily" / "2026-09-16.json").read_text(encoding="utf-8"))
    assert (first["data_quality"], first["data_quality_reason"]) == ("test_contaminated", "理由A")
    assert second["data_quality"] == "suspected_test_contaminated"
    untouched = root / "daily" / "2026-09-18.json"
    assert untouched.read_bytes() == before[untouched]
    weekly = root / "weekly" / "2026-09-14_2026-09-18.json"
    assert weekly.read_bytes() == before[weekly]
    assert not list(root.rglob("*.tmp"))


def test_apply_is_idempotent_and_keeps_the_first_marked_at(tmp_path):
    root, targets = _workspace(tmp_path)
    mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)
    after_first = _snapshot(root)

    plan = mark.run(
        True, report_root=root, backup_root=root / "second_backup",
        now=lambda: datetime(2026, 10, 4), targets=targets, output=lambda message: None,
    )

    assert [item.status for item in plan.targets] == ["already_marked", "already_marked"]
    assert {path: data for path, data in _snapshot(root).items() if "second_backup" not in str(path)} == after_first


def test_apply_aborts_when_a_backup_already_exists_and_changes_nothing(tmp_path):
    root, targets = _workspace(tmp_path)
    stale = root / mark.BACKUP_NAME / "daily" / "2026-09-16.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    before = {path: data for path, data in _snapshot(root).items() if mark.BACKUP_NAME not in str(path)}

    with pytest.raises(FileExistsError):
        mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)

    assert {path: data for path, data in _snapshot(root).items() if mark.BACKUP_NAME not in str(path)} == before
    assert stale.read_text(encoding="utf-8") == "stale"
    assert not (root / mark.BACKUP_NAME / "daily" / "2026-09-04.json").exists()


def test_report_changed_since_investigation_is_skipped(tmp_path):
    root, targets = _workspace(tmp_path)
    path = root / "daily" / "2026-09-04.json"
    _write_report(path, {**_report("2026-09-04"), "order_count": 5})
    before = path.read_bytes()

    plan = mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)

    assert plan.targets[0].status == "skip" and "SHA-256" in plan.targets[0].note
    assert path.read_bytes() == before
    assert plan.targets[1].status == "mark"


def test_lf_files_keep_lf_line_endings(tmp_path):
    root, targets = _workspace(tmp_path, newline="\n")

    mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)

    assert b"\r\n" not in (root / "daily" / "2026-09-04.json").read_bytes()


def test_period_usage_lists_how_marked_days_are_included(tmp_path):
    root, targets = _workspace(tmp_path)

    plan = mark.run(False, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)

    usage = plan.usages[0]
    assert usage.relative_path == "weekly/2026-09-14_2026-09-18.json"
    assert [row["date"] for row in usage.marked_days] == ["2026-09-16"]
    assert usage.marked_totals["order_count"] == 1
    assert usage.totals["order_count"] == 3


def test_summary_loader_ignores_the_added_fields(tmp_path):
    root, targets = _workspace(tmp_path)
    directory = root / "daily"
    before = load_daily_summaries(directory, date(2026, 9, 1), date(2026, 9, 30))

    mark.run(True, report_root=root, now=lambda: NOW, targets=targets, output=lambda message: None)
    after = load_daily_summaries(directory, date(2026, 9, 1), date(2026, 9, 30))

    assert after == before
    assert summarize_daily_reports(after) == summarize_daily_reports(before)
