import json
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.application.filtering_usecase import FilteringUseCase
from src.infrastructure.persistence.decision_journal_repository import (
    STAGE_TRADING,
    DecisionJournalRepository,
)
from test_trading_usecase_skip_reasons import _REAL_PRICE_LIMIT, _REAL_RSI, _build_loop

DAY = datetime(2026, 9, 25, 10, 0, 0)


def _rows(path, sql="SELECT * FROM decision_records ORDER BY id"):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def test_same_day_symbol_reason_aggregates_into_one_row(tmp_path):
    path = tmp_path / "journal.sqlite3"
    repository = DecisionJournalRepository(path)
    for minute in (0, 1, 2):
        repository.record(
            STAGE_TRADING, "7203", "BOARD_UNAVAILABLE", datetime(2026, 9, 25, 10, minute), {"rsi": 50.0 + minute}
        )
    repository.record(STAGE_TRADING, "7203", "INSUFFICIENT_RSI_HISTORY", DAY, {})
    repository.record(STAGE_TRADING, "6758", "BOARD_UNAVAILABLE", DAY, {})
    repository.flush()

    rows = _rows(path)
    assert len(rows) == 3
    row = next(r for r in rows if r["symbol"] == "7203" and r["reason_code"] == "BOARD_UNAVAILABLE")
    assert row["occurrence_count"] == 3
    assert row["first_occurred_at"] == "2026-09-25T10:00:00"
    assert row["last_occurred_at"] == "2026-09-25T10:02:00"
    assert row["rsi"] == 52.0
    assert json.loads(row["first_inputs_json"]) == {"rsi": 50.0}


def test_count_accumulates_across_flushes_and_restart(tmp_path):
    path = tmp_path / "journal.sqlite3"
    repository = DecisionJournalRepository(path)
    repository.record(STAGE_TRADING, "7203", "atr_danger_skip", DAY, {"atr": 1.5})
    repository.flush()
    restarted = DecisionJournalRepository(path)
    restarted.record(STAGE_TRADING, "7203", "atr_danger_skip", datetime(2026, 9, 25, 11, 0), {})
    restarted.record(STAGE_TRADING, "7203", "atr_danger_skip", datetime(2026, 9, 25, 11, 1), {})
    restarted.flush()

    (row,) = _rows(path)
    assert row["occurrence_count"] == 3
    assert row["first_occurred_at"] == "2026-09-25T10:00:00"
    assert row["last_occurred_at"] == "2026-09-25T11:01:00"
    assert row["atr"] == 1.5


def test_next_day_creates_new_row(tmp_path):
    path = tmp_path / "journal.sqlite3"
    repository = DecisionJournalRepository(path)
    repository.record(STAGE_TRADING, "7203", "BOARD_UNAVAILABLE", DAY, {})
    repository.record(STAGE_TRADING, "7203", "BOARD_UNAVAILABLE", datetime(2026, 9, 28, 9, 0), {})
    repository.flush()

    rows = _rows(path)
    assert [(r["decision_date"], r["occurrence_count"]) for r in rows] == [("2026-09-25", 1), ("2026-09-28", 1)]


def test_failed_flush_keeps_pending_records(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite3"
    repository = DecisionJournalRepository(path)
    repository.record(STAGE_TRADING, "7203", "BOARD_UNAVAILABLE", DAY, {})
    original_connect = repository._connect

    def broken_connect():
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(repository, "_connect", broken_connect)
    with pytest.raises(sqlite3.OperationalError):
        repository.flush()
    monkeypatch.setattr(repository, "_connect", original_connect)
    repository.flush()

    assert [r["occurrence_count"] for r in _rows(path)] == [1]


def test_account_snapshot_start_keeps_first_and_end_overwrites(tmp_path):
    path = tmp_path / "journal.sqlite3"
    repository = DecisionJournalRepository(path)
    repository.record_account_snapshot("start", datetime(2026, 9, 25, 9, 0), 100.0, 0, 3, [])
    repository.record_account_snapshot("start", datetime(2026, 9, 25, 9, 30), 90.0, 1, 3, [{"symbol": "1"}])
    repository.record_account_snapshot("end", datetime(2026, 9, 25, 15, 0), 80.0, 0, 3, [])
    repository.record_account_snapshot("end", datetime(2026, 9, 25, 15, 20), 70.0, 0, 3, [])

    rows = _rows(path, "SELECT * FROM daily_account_snapshots ORDER BY kind")
    assert [(r["kind"], r["wallet_amount"]) for r in rows] == [("end", 70.0), ("start", 100.0)]


def test_trading_loop_journals_rsi_shortage_once_per_day_with_counts(monkeypatch, tmp_path):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, closes=[100.0, 101.0, 102.0], price_limit=_REAL_PRICE_LIMIT, rsi=_REAL_RSI,
    )
    journal_path = tmp_path / "journal.sqlite3"
    use_case.decision_journal_repository = DecisionJournalRepository(journal_path)

    use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    rows = [r for r in _rows(journal_path) if r["reason_code"] == "INSUFFICIENT_RSI_HISTORY"]
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] > 1
    assert (rows[0]["rsi_required_closes"], rows[0]["rsi_actual_closes"]) == (5, 3)
    assert rows[0]["market_regime"] == "NORMAL"
    snapshots = _rows(journal_path, "SELECT kind FROM daily_account_snapshots ORDER BY kind")
    assert [s["kind"] for s in snapshots] == ["end", "start"]


def test_trading_loop_journals_zero_quantity_with_budget_and_account(monkeypatch, tmp_path):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, wallet_amount=1_000.0,
    )
    journal_path = tmp_path / "journal.sqlite3"
    use_case.decision_journal_repository = DecisionJournalRepository(journal_path)

    use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    assert sender.orders == []
    by_reason = {r["reason_code"]: r for r in _rows(journal_path)}
    row = by_reason["budget_below_one_lot"]
    assert row["allocated_budget"] == 1_000.0 / 3
    assert (row["quantity_before"], row["quantity_after"]) == (0, 0)
    assert row["quantity_zero_reason"] == "budget_below_one_lot"
    assert row["wallet_amount"] == 1_000.0
    assert (row["open_position_count"], row["target_positions"], row["remaining_slots"]) == (0, 3, 3)
    assert "ORDER_QUANTITY_ZERO" in by_reason


@pytest.mark.parametrize(
    "reason_code",
    [
        "ORDER_REJECTED_PAPER_PRICE_MISSING",
        "ORDER_REJECTED_PAPER_PRICE_INVALID",
    ],
)
def test_trading_loop_journals_paper_price_rejection_reason(
    monkeypatch, tmp_path, reason_code
):
    class PriceRejectedOrderSender:
        last_rejection_reason = reason_code

        def set_price(self, symbol, price):
            pass

        def place_market_order(self, token, symbol, side, quantity):
            return None

    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, order_sender=PriceRejectedOrderSender(),
    )
    journal_path = tmp_path / "journal.sqlite3"
    use_case.decision_journal_repository = DecisionJournalRepository(journal_path)

    use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    row = next(
        row for row in _rows(journal_path)
        if row["reason_code"] == reason_code
    )
    assert row["occurrence_count"] == 3
    assert row["symbol"] == "7203"


def test_journal_failure_does_not_stop_trading(monkeypatch, tmp_path):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(monkeypatch, tmp_path)

    class FailingJournal:
        def record(self, *args, **kwargs):
            raise RuntimeError("record failed")

        def flush(self):
            raise RuntimeError("flush failed")

        def record_account_snapshot(self, *args, **kwargs):
            raise RuntimeError("snapshot failed")

    use_case.decision_journal_repository = FailingJournal()

    use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    assert len(sender.orders) == 3


def test_filtering_records_stage_counts_and_skip_reasons(tmp_path):
    symbols = ["1001", "1002", "1003", "1004"]
    screening = SimpleNamespace(symbols=symbols)

    class ScreeningRepository:
        def load_for_date(self, day):
            return screening

    class BoardClient:
        def get_current_board(self, symbol):
            if symbol == "1001":
                return None
            if symbol == "1002":
                return {"current_price": 100.0}
            return {"current_price": 100.0, "trading_value": 2_000_000.0}

    class VolumeClient:
        def get_average_turnover(self, symbol, days):
            return 1_000_000.0

    class ResultRepository:
        def save(self, result):
            pass

    path = tmp_path / "journal.sqlite3"
    journal = DecisionJournalRepository(path, execution_mode="paper")
    FilteringUseCase(
        ScreeningRepository(), BoardClient(), VolumeClient(), ResultRepository(),
        decision_journal_repository=journal,
    ).execute()

    (summary,) = _rows(path, "SELECT * FROM filter_stage_summaries")
    assert (summary["input_count"], summary["evaluated_count"], summary["skipped_count"]) == (4, 2, 2)
    assert summary["selected_count"] == 2
    assert json.loads(summary["skip_reason_counts_json"]) == {
        "FILTER_BOARD_MISSING": 1,
        "FILTER_TURNOVER_MISSING": 1,
    }
    reasons = {(r["symbol"], r["reason_code"], r["stage"]) for r in _rows(path)}
    assert reasons == {
        ("1001", "FILTER_BOARD_MISSING", "filtering"),
        ("1002", "FILTER_TURNOVER_MISSING", "filtering"),
    }


def test_filtering_continues_when_journal_fails(tmp_path):
    class ScreeningRepository:
        def load_for_date(self, day):
            return SimpleNamespace(symbols=["1001"])

    class BoardClient:
        def get_current_board(self, symbol):
            return {"current_price": 100.0, "trading_value": 2_000_000.0}

    class VolumeClient:
        def get_average_turnover(self, symbol, days):
            return 1_000_000.0

    saved = []

    class ResultRepository:
        def save(self, result):
            saved.append(result)

    class FailingJournal:
        def record(self, *args, **kwargs):
            raise RuntimeError("boom")

        def flush(self):
            raise RuntimeError("boom")

        def record_filter_stage_summary(self, *args, **kwargs):
            raise RuntimeError("boom")

    result = FilteringUseCase(
        ScreeningRepository(), BoardClient(), VolumeClient(), ResultRepository(),
        decision_journal_repository=FailingJournal(),
    ).execute()

    assert result.symbols == ["1001"]
    assert len(saved) == 1
    assert date.today().isoformat() == result.date
