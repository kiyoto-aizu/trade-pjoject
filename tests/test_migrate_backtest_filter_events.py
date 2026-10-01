import sqlite3
import sys
from datetime import datetime

import pytest

from scripts.ops import migrate_backtest_filter_events as migration
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository


def _create_events(database_path):
    repository = FilterDecisionRepository(database_path)
    repository.record_event(
        "ATR_DANGER_SKIP", "7203", datetime(2026, 9, 1, 10),
        100.0, 100, execution_mode="backtest",
    )
    repository.record_event(
        "ATR_DANGER_SKIP", "8306", datetime(2026, 9, 2, 10),
        100.0, 100, execution_mode="paper",
    )
    repository.record_event(
        "ATR_DANGER_SKIP", "6758", datetime(2026, 9, 3, 10),
        100.0, 100, execution_mode="live",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE filter_decision_events SET id=900 WHERE execution_mode='backtest'"
        )
        connection.execute(
            "UPDATE filter_decision_events SET status='finalized', finalized_at=?, "
            "outcome=? WHERE execution_mode='backtest'",
            ("2026-09-08T10:00:00", "検証結果"),
        )


def _counts(database_path):
    with sqlite3.connect(database_path) as connection:
        return dict(connection.execute(
            "SELECT execution_mode, COUNT(*) FROM filter_decision_events GROUP BY execution_mode"
        ))


def test_migration_dry_run_does_not_change_databases_or_create_backup(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_events(source)

    report = migration.migrate_backtest_filter_events(
        source, destination, backup_directory,
    )

    assert report["backtest_count"] == 1
    assert report["breakdown"] == {("2026-09", "finalized"): 1}
    assert _counts(source) == {"backtest": 1, "live": 1, "paper": 1}
    assert not destination.exists()
    assert not backup_directory.exists()


def test_migration_moves_only_backtest_rows_and_preserves_backup_and_state(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_events(source)

    report = migration.migrate_backtest_filter_events(
        source, destination, backup_directory, apply=True,
    )

    assert report["copied_count"] == report["deleted_count"] == 1
    assert _counts(source) == {"live": 1, "paper": 1}
    assert _counts(destination) == {"backtest": 1}
    with sqlite3.connect(destination) as connection:
        migrated = connection.execute(
            "SELECT id, status, finalized_at, outcome FROM filter_decision_events"
        ).fetchone()
    assert migrated == (1, "finalized", "2026-09-08T10:00:00", "検証結果")

    backups = list(backup_directory.glob("filter_decision_events.*.sqlite3"))
    assert len(backups) == 1
    assert _counts(backups[0]) == {"backtest": 1, "live": 1, "paper": 1}

    second_report = migration.migrate_backtest_filter_events(
        source, destination, backup_directory, apply=True,
    )
    assert second_report["backtest_count"] == 0
    assert len(list(backup_directory.glob("filter_decision_events.*.sqlite3"))) == 1
    assert _counts(source) == {"live": 1, "paper": 1}
    assert _counts(destination) == {"backtest": 1}


def test_migration_copy_count_mismatch_keeps_source_unchanged(monkeypatch, tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    _create_events(source)
    monkeypatch.setattr(migration, "_copy_backtest_events", lambda *args: 0)

    with pytest.raises(RuntimeError, match="本番DBは変更していません"):
        migration.migrate_backtest_filter_events(
            source, destination, tmp_path / "backups", apply=True,
        )

    assert _counts(source) == {"backtest": 1, "live": 1, "paper": 1}


def test_migration_backup_failure_aborts_before_copy_or_delete(monkeypatch, tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "destination.sqlite3"
    _create_events(source)

    def fail_backup(*args, **kwargs):
        raise OSError("backup unavailable")

    monkeypatch.setattr(migration.shutil, "copy2", fail_backup)
    with pytest.raises(OSError, match="backup unavailable"):
        migration.migrate_backtest_filter_events(
            source, destination, tmp_path / "backups", apply=True,
        )

    assert _counts(source) == {"backtest": 1, "live": 1, "paper": 1}
    assert not destination.exists()


def test_duplicate_report_counts_groups_excess_rows_status_and_does_not_modify_db(tmp_path):
    database = tmp_path / "duplicates.sqlite3"
    _create_events(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE filter_decision_events SET status='observing', finalized_at=NULL "
            "WHERE execution_mode='backtest'"
        )
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(filter_decision_events)")
            if row[1] != "id"
        ]
        column_sql = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f"INSERT INTO filter_decision_events ({column_sql}) "
            f"SELECT {column_sql} FROM filter_decision_events WHERE execution_mode='backtest'"
        )
        connection.execute(
            "UPDATE filter_decision_events SET status='finalized' "
            "WHERE execution_mode='backtest' AND id=(SELECT MAX(id) FROM filter_decision_events)"
        )
    before = database.read_bytes()

    report = migration.report_backtest_duplicates(database)

    assert report == {
        "duplicate_group_count": 1,
        "excess_row_count": 1,
        "top_duplicates": [("ATR_DANGER_SKIP", "7203", "2026-09-01", 2)],
        "status_counts": {"finalized": 1, "observing": 1},
    }
    assert database.read_bytes() == before


def test_duplicate_report_cannot_be_combined_with_apply(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["migrate_backtest_filter_events.py", "--apply", "--report-duplicates"]
    )

    with pytest.raises(SystemExit) as error:
        migration.main()

    assert error.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err