import json
import sqlite3
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.infrastructure.analysis.strategy_analysis_code_sandbox import (
    SandboxValidationError,
    StrategyAnalysisCodeSandbox,
    validate_generated_code,
    validate_readonly_query,
)


def _create_data_sources(root: Path) -> tuple[Path, Path]:
    parquet_directory = root / "parquet"
    partition = parquet_directory / "symbol=1436" / "date=2026-09-21"
    partition.mkdir(parents=True)
    pq.write_table(
        pa.table({
            "time": ["09:00:00", "09:01:00"],
            "price": [100.0, 102.0],
            "cumulative_volume": [10.0, 25.0],
            "volume": [10.0, 15.0],
            "source": ["poll", "poll"],
        }),
        partition / "data.parquet",
    )
    sqlite_path = root / "events.sqlite3"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "CREATE TABLE filter_decision_events "
            "(id INTEGER, event_type TEXT, symbol TEXT, occurred_at TEXT, reference_price REAL)"
        )
        connection.execute(
            "INSERT INTO filter_decision_events VALUES (1, 'ATR_DANGER_SKIP', '1436', '2026-09-21T10:00:00', 100.0)"
        )
    return parquet_directory, sqlite_path


def test_generated_code_accepts_only_restricted_python_subset():
    validate_generated_code(
        'rows = query_parquet("SELECT COUNT(*) AS count FROM minute_bars")\nprint(rows)'
    )

    for code in (
        "import os",
        "open('data.txt', 'w')",
        "query_parquet.__globals__",
        "while True:\n    print('loop')",
        "query_parquet('SELECT 1'); open('x', 'w')",
        "values = [0]\nexpanded = values * 1000000",
        "expanded = 'x' * 1000000",
        "rows = query_filter_events(\"SELECT event_type FROM filter_decision_events\")\n"
        "expanded = rows[0]['event_type'] * 1000000",
        "value = 1000000000000000",
        "values = [value for value in unbounded]",
        "values = [(left, right) for left in bounded for right in bounded]",
        "values = [item for row in rows for item in row]",
        "for row in rows:\n    values = [item for item in rows]\n",
    ):
        with pytest.raises(SandboxValidationError):
            validate_generated_code(code)


def test_readonly_sql_validation_rejects_writes_external_sources_and_unknown_tables():
    validate_readonly_query(
        "SELECT event_type, COUNT(*) AS total FROM filter_decision_events GROUP BY event_type",
        {"filter_decision_events"},
    )
    for query in (
        "DELETE FROM filter_decision_events",
        "SELECT * FROM read_parquet('data.parquet')",
        "SELECT * FROM secret_table",
        "SELECT * FROM main.filter_decision_events",
        "SELECT 1; SELECT 2",
    ):
        with pytest.raises(SandboxValidationError):
            validate_readonly_query(query, {"filter_decision_events"})


def test_readonly_sql_validation_accepts_audited_readonly_string_and_time_functions():
    validate_readonly_query(
        "SELECT SUBSTRING(occurred_at, 1, 10) AS event_date "
        "FROM filter_decision_events",
        {"filter_decision_events"},
    )
    validate_readonly_query(
        "SELECT STRFTIME(TIME '12:00:00', '%H:%M:%S') AS event_time",
        set(),
    )
    validate_readonly_query(
        "SELECT SUM(CASE WHEN status = 'finalized' AND outcome IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM filter_decision_events WHERE status = 'finalized' OR outcome IS NOT NULL",
        {"filter_decision_events"},
    )


def test_child_process_queries_period_parquet_and_sqlite_as_readonly_data(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    sandbox = StrategyAnalysisCodeSandbox(parquet_directory, sqlite_path)
    code = (
        'print(query_parquet("SELECT symbol, COUNT(*) AS bars, AVG(price) AS avg_price "'
        ' "FROM minute_bars GROUP BY symbol"))\n'
        'print(query_filter_events("SELECT event_type, COUNT(*) AS events "'
        ' "FROM filter_decision_events GROUP BY event_type"))'
    )

    result = sandbox.run(code, date(2026, 9, 21), date(2026, 9, 21))

    assert result.status == "succeeded"
    assert "'bars': 2" in result.stdout
    assert "'avg_price': 101.0" in result.stdout
    assert "ATR_DANGER_SKIP" in result.stdout
    with sqlite3.connect(sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM filter_decision_events").fetchone()[0] == 1


def test_child_process_runs_bounded_sample_size_analysis_from_query_rows(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "INSERT INTO filter_decision_events VALUES "
            "(2, 'ATR_DANGER_SKIP', '1436', '2026-09-21T11:00:00', 110.0)"
        )
    code = (
        'rows = query_filter_events("SELECT reference_price FROM filter_decision_events ORDER BY id")\n'
        "values = [float(row.get('reference_price')) for row in rows "
        "if row.get('reference_price') is not None]\n"
        "sample_count = len(values)\n"
        "if sample_count >= 2:\n"
        "    mean_value = sum(values) / sample_count\n"
        "    variance = sum((value - mean_value) ** 2 for value in values) / (sample_count - 1)\n"
        "    standard_deviation = variance ** 0.5\n"
        "    effects = [0.5 * standard_deviation, 0.25 * standard_deviation, "
        "0.1 * standard_deviation]\n"
        "    required_samples = []\n"
        "    for effect in effects:\n"
        "        required_samples.append(int(round(((2.8 * standard_deviation) / effect) ** 2)))\n"
        "    print({'sample_count': sample_count, 'mean': round(mean_value, 2), "
        "'standard_deviation': round(standard_deviation, 2), "
        "'required_samples': required_samples})\n"
    )

    result = StrategyAnalysisCodeSandbox(parquet_directory, sqlite_path).run(
        code, date(2026, 9, 21), date(2026, 9, 21)
    )

    assert result.status == "succeeded"
    assert "'sample_count': 2" in result.stdout
    assert "'mean': 105.0" in result.stdout
    assert "'standard_deviation': 7.07" in result.stdout
    assert "'required_samples': [31, 125, 784]" in result.stdout


def test_child_process_returns_failure_without_stopping_caller(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    result = StrategyAnalysisCodeSandbox(parquet_directory, sqlite_path).run(
        'query_filter_events("DELETE FROM filter_decision_events")',
        date(2026, 9, 21),
        date(2026, 9, 21),
    )

    assert result.status == "rejected"
    assert "単一のSELECT文" in result.stderr


def test_child_process_timeout_is_reported(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    sandbox = StrategyAnalysisCodeSandbox(
        parquet_directory, sqlite_path, timeout_seconds=0.001
    )

    result = sandbox.run("print(1)", date(2026, 9, 21), date(2026, 9, 21))

    assert result.timed_out is True
    assert result.status == "timed_out"


def test_parquet_path_selection_is_inclusive_by_date(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    sandbox = StrategyAnalysisCodeSandbox(parquet_directory, sqlite_path)

    paths = sandbox.parquet_paths(date(2026, 9, 20), date(2026, 9, 22))

    assert len(paths) == 1
    assert paths[0].parts[-2] == "date=2026-09-21"


def test_schema_description_and_fingerprints_use_real_period_data(tmp_path):
    parquet_directory, sqlite_path = _create_data_sources(tmp_path)
    sandbox = StrategyAnalysisCodeSandbox(parquet_directory, sqlite_path)

    schema = sandbox.describe_data_sources(date(2026, 9, 21), date(2026, 9, 21))

    assert schema["parquet"]["files_in_period"] == 1
    assert {item["column"] for item in schema["parquet"]["columns"]} >= {
        "time", "price", "volume", "source", "date", "symbol",
    }
    assert {item["column"] for item in schema["sqlite"]["columns"]} == {
        "id", "event_type", "symbol", "occurred_at", "reference_price",
    }
    assert sandbox.compare_source_fingerprints(schema) == {"matches": True, "mismatches": []}

    parquet_file = schema["parquet"]["sha256_by_file"][0]["path"]
    with open(parquet_file, "ab") as stream:
        stream.write(b"changed")

    comparison = sandbox.compare_source_fingerprints(schema)
    assert comparison["matches"] is False
    assert parquet_file in comparison["mismatches"]