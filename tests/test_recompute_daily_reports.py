import json
from datetime import datetime

import pytest

from scripts.ops import recompute_daily_reports as recompute

NOW = datetime(2026, 10, 3, 12, 0, 0)


def _order(day, time, symbol, side, price, qty):
    return {
        "symbol": symbol, "side": side, "price": price, "qty": qty,
        "timestamp": f"{day}T{time}", "result_code": 0,
    }


def _replay(history):
    return recompute.replay_order_history(history, 100_000.0, fee_rate=0.0, market_slippage_bps=0.0)


def test_replay_empty_history_has_no_holdings_and_unchanged_cash():
    result = _replay([])

    assert result.daily_realized == {}
    assert result.final_cash == 100_000.0
    assert result.holdings == {}


def test_replay_buy_only_day_realizes_nothing_and_keeps_the_holding():
    result = _replay([_order("2026-09-25", "09:54:00", "3807", "2", 93.0, 100)])

    assert result.daily_realized == {"2026-09-25": 0.0}
    assert result.holdings == {"3807": 100}
    assert result.final_cash == pytest.approx(100_000.0 - 9_300.0)


def test_replay_sell_without_holding_is_reported_and_not_filled():
    result = _replay([_order("2026-09-28", "15:20:00", "3807", "1", 92.0, 100)])

    assert result.daily_realized == {"2026-09-28": 0.0}
    assert result.final_cash == 100_000.0
    assert result.warnings and "約定しませんでした" in result.warnings[0]


def test_replay_attributes_realized_pnl_to_the_sell_day_across_days():
    result = _replay([
        _order("2026-09-25", "09:54:00", "3807", "2", 93.0, 100),
        _order("2026-09-28", "11:33:00", "3807", "2", 93.0, 100),
        _order("2026-09-28", "15:20:00", "3807", "1", 92.0, 200),
        _order("2026-10-01", "10:00:00", "4597", "2", 33.0, 400),
    ])

    assert result.daily_realized["2026-09-25"] == 0.0
    assert result.daily_realized["2026-09-28"] == pytest.approx(-200.0)
    assert result.daily_realized["2026-10-01"] == 0.0
    assert result.order_counts == {"2026-09-25": 1, "2026-09-28": 2, "2026-10-01": 1}


def test_replay_includes_fee_and_slippage_from_the_client_settings():
    history = [
        _order("2026-09-14", "11:00:00", "4564", "2", 100.0, 100),
        _order("2026-09-14", "15:20:00", "4564", "1", 100.0, 100),
    ]

    result = recompute.replay_order_history(history, 100_000.0, fee_rate=0.001, market_slippage_bps=10.0)

    # 買い100.1 / 売り99.9 の差額-20と、約定額にかかる手数料(10.01 + 9.99)。
    assert result.daily_realized["2026-09-14"] == pytest.approx(-40.0, abs=0.01)


def test_replay_uses_last_known_price_when_the_history_price_is_zero():
    result = _replay([
        _order("2026-10-02", "09:35:00", "4597", "2", 33.0, 400),
        _order("2026-10-02", "15:08:00", "4597", "1", 0.0, 400),
    ])

    assert result.daily_realized["2026-10-02"] == pytest.approx(0.0)
    assert result.holdings == {}
    assert any("価格が0" in warning for warning in result.warnings)


def test_recomputed_report_changes_only_the_profit_items_and_adds_metadata():
    report = {
        "date": "2026-09-28", "order_count": 6, "orders": [{"symbol": "3807"}],
        "positions": [], "total_profit_loss": 0, "kill_switch_triggered": False,
        "report_text": "合計損益: 0円", "llm_analysis": None,
    }

    result = recompute.recomputed_report(report, 362.77, "2026-10-03T12:00:00")

    assert list(result)[:7] == [
        "date", "order_count", "orders", "positions", "realized_profit_loss",
        "unrealized_profit_loss", "total_profit_loss",
    ]
    assert (result["realized_profit_loss"], result["unrealized_profit_loss"], result["total_profit_loss"]) == (
        362.77, 0.0, 362.77,
    )
    assert result["original_total_profit_loss"] == 0
    assert result["recomputed"] is True
    assert result["recomputed_at"] == "2026-10-03T12:00:00"
    assert result["recompute_source"]
    unchanged = {key: value for key, value in result.items() if key in report and not key.endswith("_loss")}
    assert unchanged == {key: value for key, value in report.items() if not key.endswith("_loss")}


def test_recomputed_report_keeps_unrealized_from_recorded_positions_and_is_idempotent():
    report = {
        "date": "2026-09-25", "total_profit_loss": 0,
        "positions": [{"symbol": "3807", "profit_loss": -50.0}],
    }

    first = recompute.recomputed_report(report, 10.0, "t1")
    second = recompute.recomputed_report(first, 10.0, "t2")

    assert first["unrealized_profit_loss"] == -50.0
    assert first["total_profit_loss"] == -40.0
    assert second["total_profit_loss"] == -40.0
    assert second["original_total_profit_loss"] == 0


def _weekly(total_by_day):
    reports = [{"date": day, "total_profit_loss": total} for day, total in total_by_day.items()]
    total = round(sum(total_by_day.values()), 2)
    return {
        "week_start": "2026-09-14", "week_end": "2026-09-18",
        "period": {"start": "2026-09-14", "end": "2026-09-18", "as_of": "2026-09-19"},
        "daily": {"report_count": len(reports), "total_profit_loss": total, "reports": reports},
        "comparison": {
            "current": {"total_profit_loss": total},
            "previous": {"period": {"start": "2026-09-07", "end": "2026-09-11"}, "total_profit_loss": 0},
        },
        "llm_analysis": "keep",
    }


def test_patch_period_report_replaces_only_profit_values():
    original = _weekly({"2026-09-14": 0, "2026-09-15": 0, "2026-09-18": 0})

    updated, problems = recompute.patch_period_report(
        original, {"2026-09-14": -4.83, "2026-09-18": -50.4}, {"2026-09-14": 0.0, "2026-09-18": 0.0}
    )

    assert problems == []
    assert updated["daily"]["total_profit_loss"] == -55.23
    assert updated["comparison"]["current"]["total_profit_loss"] == -55.23
    assert updated["llm_analysis"] == "keep"
    assert all(path.rsplit(".", 1)[-1] == "total_profit_loss" for path in recompute._differences(original, updated))


def test_patch_period_report_flags_inconsistent_original_and_missing_day():
    original = _weekly({"2026-09-14": 0})
    original["daily"]["total_profit_loss"] = 5.0

    _, problems = recompute.patch_period_report(original, {"2026-09-16": 1.0}, {"2026-09-16": 0.0})

    assert len(problems) == 2


def test_patch_period_report_adjusts_previous_total_for_the_following_period():
    following = _weekly({"2026-09-21": 0})
    following["period"] = {"start": "2026-09-21", "end": "2026-09-25", "as_of": "2026-09-26"}
    following["comparison"]["previous"] = {
        "period": {"start": "2026-09-14", "end": "2026-09-18"}, "total_profit_loss": 0,
    }

    updated, _ = recompute.patch_period_report(following, {"2026-09-14": -4.83}, {"2026-09-14": 0.0})

    assert updated["comparison"]["previous"]["total_profit_loss"] == -4.83
    assert updated["daily"]["total_profit_loss"] == 0


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


def _workspace(tmp_path):
    root = tmp_path / "reports"
    history = [
        _order("2026-09-14", "11:00:00", "4564", "2", 100.0, 100),
        _order("2026-09-14", "15:20:00", "4564", "1", 90.0, 100),
    ]
    history_path = tmp_path / "order_history.json"
    _write(history_path, history)
    _write(root / "daily" / "2026-09-14.json", {
        "date": "2026-09-14", "order_count": 2, "orders": [], "positions": [],
        "total_profit_loss": 0, "report_text": "x",
    })
    weekly = _weekly({"2026-09-14": 0, "2026-09-15": 0})
    _write(root / "weekly" / "2026-09-14_2026-09-18.json", weekly)
    _write(root / "monthly" / "2026-09.json", {
        "month": "2026-09",
        "period": {"start": "2026-09-01", "end": "2026-09-30", "as_of": "2026-09-30"},
        "daily": {"total_profit_loss": 0, "reports": [{"date": "2026-09-14", "total_profit_loss": 0}]},
        "comparison": {
            "current": {"total_profit_loss": 0},
            "previous": {"period": {"start": "2026-08-01", "end": "2026-08-31"}, "total_profit_loss": 0},
        },
    })
    return root, history_path


def _snapshot(root):
    return {path: path.read_bytes() for path in sorted(root.rglob("*.json"))}


def _run(tmp_path, root, history_path, apply):
    messages = []
    plan = recompute.run(
        apply, report_root=root, order_history_path=history_path,
        state_path=tmp_path / "missing_state.json", target_dates=("2026-09-14",),
        check_dates=(), now=lambda: NOW, initial_cash=100_000.0, output=messages.append,
    )
    return plan, messages


def test_following_week_previous_total_is_aligned_and_nothing_else_changes(tmp_path):
    root, history_path = _workspace(tmp_path)
    following = _weekly({"2026-09-21": 0})
    following.update({"week_start": "2026-09-21", "week_end": "2026-09-25"})
    following["period"] = {"start": "2026-09-21", "end": "2026-09-25", "as_of": "2026-09-26"}
    following["comparison"]["previous"] = {
        "period": {"start": "2026-09-14", "end": "2026-09-18"}, "total_profit_loss": 0,
    }
    _write(root / "weekly" / "2026-09-21_2026-09-25.json", following)

    plan, _ = _run(tmp_path, root, history_path, apply=True)

    updated = json.loads((root / "weekly" / "2026-09-21_2026-09-25.json").read_text(encoding="utf-8"))
    week = json.loads((root / "weekly" / "2026-09-14_2026-09-18.json").read_text(encoding="utf-8"))
    assert updated["comparison"]["previous"]["total_profit_loss"] == week["daily"]["total_profit_loss"] < 0
    following["comparison"]["previous"]["total_profit_loss"] = week["daily"]["total_profit_loss"]
    assert updated == following
    item = next(entry for entry in plan.period_files if entry.relative_path.endswith("2026-09-21_2026-09-25.json"))
    assert item.changed_paths == ["comparison.previous.total_profit_loss"]


def test_dry_run_writes_nothing(tmp_path):
    root, history_path = _workspace(tmp_path)
    before = _snapshot(root)

    plan, messages = _run(tmp_path, root, history_path, apply=False)

    assert _snapshot(root) == before
    assert not (root / "_backup_20261003").exists()
    assert not list(root.rglob("*.tmp"))
    assert {item.relative_path for item in plan.daily_files + plan.period_files if item.will_write} == {
        "daily/2026-09-14.json", "weekly/2026-09-14_2026-09-18.json", "monthly/2026-09.json",
    }
    assert "ドライラン" in messages[0]


def test_apply_creates_backup_before_overwriting_and_keeps_other_fields(tmp_path, monkeypatch):
    root, history_path = _workspace(tmp_path)
    before = _snapshot(root)
    backup_root = root / "_backup_20261003"
    seen_backups = []
    real_write = recompute._atomic_write

    def checking_write(path, data):
        relative = path.relative_to(root)
        seen_backups.append((relative.as_posix(), (backup_root / relative).read_bytes() == path.read_bytes()))
        real_write(path, data)

    monkeypatch.setattr(recompute, "_atomic_write", checking_write)

    _run(tmp_path, root, history_path, apply=True)

    assert seen_backups and all(matches for _, matches in seen_backups)
    for path, content in before.items():
        relative = path.relative_to(root)
        if relative.parts[0] in ("daily", "weekly", "monthly"):
            assert (backup_root / relative).read_bytes() == content
    daily = json.loads((root / "daily" / "2026-09-14.json").read_text(encoding="utf-8"))
    assert daily["total_profit_loss"] < -1_000.0
    assert daily["recomputed"] is True
    assert daily["order_count"] == 2 and daily["report_text"] == "x"
    weekly = json.loads((root / "weekly" / "2026-09-14_2026-09-18.json").read_text(encoding="utf-8"))
    assert weekly["daily"]["total_profit_loss"] == daily["total_profit_loss"]
    assert weekly["llm_analysis"] == "keep"


def test_apply_refuses_to_overwrite_an_existing_backup(tmp_path):
    root, history_path = _workspace(tmp_path)
    backup_root = root / "_backup_20261003"
    _write(backup_root / "daily" / "2026-09-14.json", {"stale": True})
    before = _snapshot(root)

    with pytest.raises(FileExistsError):
        _run(tmp_path, root, history_path, apply=True)

    assert {path: data for path, data in _snapshot(root).items() if "_backup_" not in str(path)} == {
        path: data for path, data in before.items() if "_backup_" not in str(path)
    }
