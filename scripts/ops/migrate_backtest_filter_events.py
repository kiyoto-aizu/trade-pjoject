"""本番判定イベントDBからバックテスト行を検証用DBへ移す一回限りの操作。"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

logger = logging.getLogger(__name__)
TABLE_NAME = "filter_decision_events"


def _connect_readonly(database_path: Path) -> sqlite3.Connection:
    if not database_path.is_file():
        raise FileNotFoundError(f"データベースがありません: {database_path}")
    connection = sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _count_by_execution_mode(database_path: Path) -> dict[str, int]:
    with closing(_connect_readonly(database_path)) as connection:
        rows = connection.execute(
            f"SELECT execution_mode, COUNT(*) AS count FROM {TABLE_NAME} "
            "GROUP BY execution_mode ORDER BY execution_mode"
        )
        return {row["execution_mode"]: row["count"] for row in rows}


def _backtest_breakdown(database_path: Path) -> dict[tuple[str, str], int]:
    with closing(_connect_readonly(database_path)) as connection:
        rows = connection.execute(
            f"SELECT substr(occurred_at, 1, 7) AS month, status, COUNT(*) AS count "
            f"FROM {TABLE_NAME} WHERE execution_mode = 'backtest' "
            "GROUP BY month, status ORDER BY month, status"
        )
        return {(row["month"], row["status"]): row["count"] for row in rows}


def report_backtest_duplicates(database_path: Path) -> dict[str, object]:
    """バックテスト行の重複状況を読み取り専用で集計します。"""
    with closing(_connect_readonly(database_path)) as connection:
        duplicate_rows = connection.execute(
            f"SELECT event_type, symbol, substr(occurred_at, 1, 10) AS event_date, "
            f"COUNT(*) AS count FROM {TABLE_NAME} "
            "WHERE execution_mode = 'backtest' "
            "GROUP BY event_type, symbol, event_date HAVING COUNT(*) > 1 "
            "ORDER BY count DESC, event_type, symbol, event_date LIMIT 10"
        ).fetchall()
        duplicate_group_count, excess_row_count = connection.execute(
            f"SELECT COUNT(*), COALESCE(SUM(event_count - 1), 0) FROM ("
            f"SELECT COUNT(*) AS event_count FROM {TABLE_NAME} "
            "WHERE execution_mode = 'backtest' "
            "GROUP BY event_type, symbol, substr(occurred_at, 1, 10) "
            "HAVING COUNT(*) > 1)"
        ).fetchone()
        status_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                f"SELECT status, COUNT(*) AS count FROM {TABLE_NAME} "
                "WHERE execution_mode = 'backtest' GROUP BY status ORDER BY status"
            )
        }
    return {
        "duplicate_group_count": duplicate_group_count,
        "excess_row_count": excess_row_count,
        "top_duplicates": [
            (row["event_type"], row["symbol"], row["event_date"], row["count"])
            for row in duplicate_rows
        ],
        "status_counts": status_counts,
    }


def _copy_backtest_events(source_path: Path, destination_path: Path) -> int:
    with closing(_connect_readonly(source_path)) as source:
        table_info = source.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()
        columns = [row["name"] for row in table_info if row["name"] != "id"]
        if not columns or "execution_mode" not in columns:
            raise RuntimeError(f"{TABLE_NAME}のスキーマに必要な列がありません")
        column_sql = ", ".join(f'"{column}"' for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = source.execute(
            f"SELECT {column_sql} FROM {TABLE_NAME} "
            "WHERE execution_mode = 'backtest' ORDER BY id"
        ).fetchall()

    with closing(sqlite3.connect(destination_path)) as destination:
        with destination:
            cursor = destination.executemany(
                f"INSERT INTO {TABLE_NAME} ({column_sql}) VALUES ({placeholders})",
                [tuple(row[column] for column in columns) for row in rows],
            )
            copied_count = cursor.rowcount
    return copied_count


def _delete_backtest_events(source_path: Path, expected_count: int) -> int:
    with closing(sqlite3.connect(source_path)) as source:
        with source:
            source.execute("BEGIN IMMEDIATE")
            cursor = source.execute(
                f"DELETE FROM {TABLE_NAME} WHERE execution_mode = 'backtest'"
            )
            deleted_count = cursor.rowcount
            if deleted_count != expected_count:
                raise RuntimeError(
                    f"本番DB削除件数が移行対象数と一致しません: "
                    f"expected={expected_count}, deleted={deleted_count}"
                )
    return deleted_count


def migrate_backtest_filter_events(
    source_path: Path,
    destination_path: Path,
    backup_directory: Path,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """バックテスト行を移行し、ドライラン／実行結果を返します。"""
    source_counts = _count_by_execution_mode(source_path)
    backtest_count = source_counts.get("backtest", 0)
    breakdown = _backtest_breakdown(source_path)
    report: dict[str, object] = {
        "source_counts": source_counts,
        "backtest_count": backtest_count,
        "breakdown": breakdown,
        "remaining_count": sum(source_counts.values()) - backtest_count,
        "applied": False,
        "backup_path": None,
    }

    if not apply or backtest_count == 0:
        return report

    backup_directory.mkdir(parents=True, exist_ok=True)
    backup_path = backup_directory / (
        f"filter_decision_events.{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    )
    if backup_path.exists():
        raise FileExistsError(f"バックアップ先が既に存在します: {backup_path}")
    shutil.copy2(source_path, backup_path)

    FilterDecisionRepository(destination_path)
    copied_count = _copy_backtest_events(source_path, destination_path)
    if copied_count != backtest_count:
        raise RuntimeError(
            f"検証用DBへのコピー件数が一致しません: "
            f"source={backtest_count}, copied={copied_count}; 本番DBは変更していません"
        )

    deleted_count = _delete_backtest_events(source_path, backtest_count)
    destination_counts = _count_by_execution_mode(destination_path)
    report.update({
        "applied": True,
        "copied_count": copied_count,
        "deleted_count": deleted_count,
        "backup_path": backup_path,
        "source_counts_after": _count_by_execution_mode(source_path),
        "destination_counts_after": destination_counts,
    })
    return report


def _log_report(report: dict[str, object]) -> None:
    source_counts = report["source_counts"]
    breakdown = report["breakdown"]
    logger.info("本番DB execution_mode別件数: %s", source_counts)
    logger.info("backtest対象件数: %s", report["backtest_count"])
    if breakdown:
        logger.info("backtestの月別・status別内訳:")
        for (month, status), count in breakdown.items():
            logger.info("  %s status=%s: %s件", month, status, count)
    else:
        logger.info("backtestの月別・status別内訳: なし")
    logger.info("移行後に本番DBへ残る件数見込み: %s", report["remaining_count"])
    if report["applied"]:
        logger.info("バックアップ: %s", report["backup_path"])
        logger.info("移行件数: copied=%s deleted=%s", report["copied_count"], report["deleted_count"])
        logger.info("移行後の本番DB execution_mode別件数: %s", report["source_counts_after"])
        logger.info("移行後の検証用DB execution_mode別件数: %s", report["destination_counts_after"])
    else:
        logger.info("ドライランです。DBは変更していません。")


def _log_duplicate_report(report: dict[str, object]) -> None:
    logger.info("重複している(event_type, symbol, 発生日)組数: %s", report["duplicate_group_count"])
    logger.info("重複による余剰行数: %s", report["excess_row_count"])
    logger.info("重複数が多い上位10組:")
    for event_type, symbol, event_date, count in report["top_duplicates"]:
        logger.info("  %s %s %s: %s件", event_type, symbol, event_date, count)
    logger.info("backtest行のstatus別件数: %s", report["status_counts"])


def main() -> None:
    parser = argparse.ArgumentParser(description="backtest判定イベントを検証用DBへ移行します")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", action="store_true", help="指定時のみバックアップ後に移行します")
    operation.add_argument(
        "--report-duplicates", action="store_true", help="重複状況を読み取り専用で表示します"
    )
    parser.add_argument(
        "--database", type=Path, default=config.FILTER_DECISION_DATABASE_FILE,
        help="確認・移行元DB（既定: 本番DB）",
    )
    args = parser.parse_args()
    configure_logging()
    if args.report_duplicates:
        _log_duplicate_report(report_backtest_duplicates(args.database))
        return
    report = migrate_backtest_filter_events(
        args.database,
        config.LEGACY_BACKTEST_FILTER_DECISION_DATABASE_FILE,
        PROJECT_ROOT / "data" / "state" / "backups",
        apply=args.apply,
    )
    _log_report(report)


if __name__ == "__main__":
    main()