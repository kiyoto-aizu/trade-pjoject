from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from sqlglot import exp, parse

MAX_CODE_BYTES = 32_000
MAX_AST_NODES = 2_000
MAX_INTEGER_CONSTANT = 1_000_000
MAX_QUERY_ROWS = 1_000
MAX_LITERAL_ITERATION_ITEMS = 100
MAX_COMPREHENSIONS = 4
MAX_PYTHON_LOOPS = 8
MAX_OUTPUT_BYTES = 256_000
DUCKDB_MEMORY_LIMIT = "512MB"
ALLOWED_DATA_FUNCTIONS = frozenset({
    "ABS", "AVG", "CAST", "COALESCE", "COUNT", "DATE_PART", "DATE_TRUNC", "GREATEST",
    "LEAST", "LOWER", "MAX", "MIN", "NULLIF", "ROUND", "STRFTIME", "SUBSTR",
    "SUBSTRING", "SUM", "TIME_TO_STR", "UPPER",
})
SAFE_BUILTINS = frozenset({"abs", "float", "int", "len", "max", "min", "round", "sorted", "str", "sum"})
ALLOWED_CALLS = SAFE_BUILTINS | {"print", "query_parquet", "query_filter_events"}
_ALLOWED_AST_NODES = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Call,
    ast.Attribute,
    ast.keyword,
    ast.Subscript,
    ast.Slice,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Div,
    ast.FloorDiv,
    ast.Mult,
    ast.Mod,
    ast.Pow,
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Is,
    ast.IsNot,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.If,
    ast.For,
    ast.Break,
    ast.Continue,
    ast.GeneratorExp,
    ast.ListComp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
)


class SandboxValidationError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyAnalysisExecutionResult:
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool
    return_code: int | None
    status: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_generated_code(code: str) -> None:
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise SandboxValidationError(f"生成コードが上限({MAX_CODE_BYTES} bytes)を超えています")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise SandboxValidationError(f"生成コードの構文エラー: {exc}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise SandboxValidationError(f"生成コードの構文要素が上限({MAX_AST_NODES})を超えています")
    sequence_names: set[str] = set()
    for _ in range(len(nodes)):
        changed = False
        for node in nodes:
            if not isinstance(node, ast.Assign) or not _is_sequence_expression(node.value, sequence_names):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in sequence_names:
                    sequence_names.add(target.id)
                    changed = True
        if not changed:
            break
    bounded_names: set[str] = set()
    for _ in range(len(nodes)):
        changed = False
        for node in nodes:
            if not isinstance(node, ast.Assign) or not _is_bounded_iterable(node.value, bounded_names):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in bounded_names:
                    bounded_names.add(target.id)
                    changed = True
        if not changed:
            break
        literal_list_names = {
            target.id
            for node in nodes
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.List)
            and len(node.value.elts) <= MAX_LITERAL_ITERATION_ITEMS
            for target in node.targets
            if isinstance(target, ast.Name)
        }
    iteration_names: set[str] = set()
    comprehensions = [node for node in nodes if isinstance(node, (ast.ListComp, ast.GeneratorExp))]
    loops = [node for node in nodes if isinstance(node, ast.For)]
    if len(comprehensions) > MAX_COMPREHENSIONS:
        raise SandboxValidationError(f"内包表記が上限({MAX_COMPREHENSIONS})を超えています")
    if len(loops) > MAX_PYTHON_LOOPS:
        raise SandboxValidationError(f"Pythonループが上限({MAX_PYTHON_LOOPS})を超えています")
    for loop in loops:
        if any(
            nested is not loop and isinstance(nested, (ast.For, ast.ListComp, ast.GeneratorExp))
            for nested in ast.walk(loop)
        ):
            raise SandboxValidationError("Pythonループの入れ子は許可されていません")
    for comprehension in comprehensions:
        if any(
            isinstance(nested, (ast.ListComp, ast.GeneratorExp))
            for nested in ast.walk(comprehension.elt)
        ):
            raise SandboxValidationError("内包表記の入れ子は許可されていません")
    for node in [*loops, *[generator for comp in comprehensions for generator in comp.generators]]:
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            if len(node.generators) != 1:
                raise SandboxValidationError("内包表記は単一の反復元に限定されています")
            continue
        if not _is_bounded_iterable(node.iter, bounded_names):
            raise SandboxValidationError("ループはクエリ結果または100件以下のリテラル配列だけを反復できます")
        iteration_names.update(_target_names(node.target))
    for comp in comprehensions:
        generator = comp.generators[0]
        if not _is_bounded_iterable(generator.iter, bounded_names):
            raise SandboxValidationError("内包表記はクエリ結果または100件以下のリテラル配列だけを反復できます")
        iteration_names.update(_target_names(generator.target))
    for node in nodes:
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise SandboxValidationError(f"許可されていないPython構文です: {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise SandboxValidationError("特殊属性・内部名は使用できません")
            if isinstance(node.ctx, ast.Store) and node.id in ALLOWED_CALLS:
                raise SandboxValidationError(f"保護された関数名は代入できません: {node.id}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                is_bounded_get = (
                    node.func.attr == "get"
                    and isinstance(receiver, ast.Name)
                    and receiver.id in iteration_names
                    and not node.keywords
                    and 1 <= len(node.args) <= 2
                )
                is_local_list_append = (
                    node.func.attr == "append"
                    and isinstance(receiver, ast.Name)
                    and receiver.id in literal_list_names
                    and not node.keywords
                    and len(node.args) == 1
                )
                if not (is_bounded_get or is_local_list_append):
                    raise SandboxValidationError("許可されていないメソッド呼び出しです")
                continue
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_CALLS:
                raise SandboxValidationError("呼び出せる関数は許可リスト内に限定されています")
            if node.func.id in {"query_parquet", "query_filter_events"}:
                if len(node.args) != 1 or node.keywords or not isinstance(node.args[0], ast.Constant):
                    raise SandboxValidationError("データクエリは固定SQL文字列を1つ指定してください")
                if not isinstance(node.args[0].value, str):
                    raise SandboxValidationError("データクエリは文字列で指定してください")
                table = "minute_bars" if node.func.id == "query_parquet" else "filter_decision_events"
                validate_readonly_query(node.args[0].value, {table})
        if isinstance(node, ast.Attribute):
            is_bounded_get = (
                node.attr == "get"
                and isinstance(node.value, ast.Name)
                and node.value.id in iteration_names
            )
            is_local_list_append = (
                node.attr == "append"
                and isinstance(node.value, ast.Name)
                and node.value.id in literal_list_names
            )
            if not (is_bounded_get or is_local_list_append):
                raise SandboxValidationError("許可されていない属性アクセスです")
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            if len(node.value) > MAX_CODE_BYTES:
                raise SandboxValidationError("文字列定数が長すぎます")
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and abs(node.value) > MAX_INTEGER_CONSTANT:
            raise SandboxValidationError("整数定数が大きすぎます")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            if _is_sequence_expression(node.left, sequence_names) or _is_sequence_expression(
                node.right, sequence_names
            ):
                raise SandboxValidationError("リスト・文字列などの反復乗算は許可されていません")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            if (
                not isinstance(node.right, ast.Constant)
                or not isinstance(node.right.value, (int, float))
                or abs(node.right.value) > 2
            ):
                raise SandboxValidationError("累乗指数は±2以内の数値リテラルに限定されています")


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in target.elts))
    return set()


def _is_bounded_iterable(node: ast.AST, bounded_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bounded_names
    if isinstance(node, (ast.List, ast.Tuple)):
        return len(node.elts) <= MAX_LITERAL_ITERATION_ITEMS
    if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"query_parquet", "query_filter_events", "sorted"}
    return False


def _is_sequence_expression(node: ast.AST, sequence_names: set[str]) -> bool:
    if isinstance(node, (ast.List, ast.Tuple, ast.GeneratorExp, ast.JoinedStr)):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return True
    if isinstance(node, ast.Name):
        return node.id in sequence_names
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"query_parquet", "query_filter_events", "sorted", "str"}
    if isinstance(node, ast.Subscript):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_sequence_expression(node.left, sequence_names) or _is_sequence_expression(
            node.right, sequence_names
        )
    return False


def validate_readonly_query(query: str, allowed_tables: set[str]) -> None:
    try:
        expressions = parse(query, read="duckdb")
    except Exception as exc:
        raise SandboxValidationError(f"SQLを解析できません: {exc}") from exc
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
        raise SandboxValidationError("SQLは単一のSELECT文に限定されています")
    expression = expressions[0]
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    for table in expression.find_all(exp.Table):
        table_name = table.name.lower()
        if table.db or table.catalog or table_name not in allowed_tables | cte_names:
            raise SandboxValidationError(f"許可されていないテーブル参照です: {table.sql()}")
    for function in expression.find_all(exp.Func):
        if isinstance(function, (exp.Case, exp.If, exp.And, exp.Or)):
            continue
        if function.sql_name().upper() not in ALLOWED_DATA_FUNCTIONS:
            raise SandboxValidationError(f"許可されていないSQL関数です: {function.sql_name()}")
    with_clause = expression.args.get("with_")
    if with_clause is not None and with_clause.args.get("recursive"):
        raise SandboxValidationError("再帰CTEは許可されていません")


_WORKER_SOURCE = r'''from __future__ import annotations
import ast
import json
import math
import sqlite3
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
from sqlglot import exp, parse

MAX_QUERY_ROWS = 1000
MAX_OUTPUT_BYTES = 256000
ALLOWED_DATA_FUNCTIONS = {
    "ABS", "AVG", "CAST", "COALESCE", "COUNT", "DATE_PART", "DATE_TRUNC", "GREATEST",
    "LEAST", "LOWER", "MAX", "MIN", "NULLIF", "ROUND", "STRFTIME", "SUBSTR",
    "SUBSTRING", "SUM", "TIME_TO_STR", "UPPER",
}
SAFE_BUILTINS = {
    "abs": abs, "float": float, "int": int, "len": len, "max": max, "min": min,
    "round": round, "sorted": sorted, "str": str, "sum": sum,
}

class OutputLimitExceeded(Exception):
    pass

class LimitedWriter:
    def __init__(self, stream):
        self.stream = stream
        self.written = 0
    def write(self, value):
        encoded = value.encode("utf-8", errors="replace")
        if self.written + len(encoded) > MAX_OUTPUT_BYTES:
            raise OutputLimitExceeded("stdout が256000 bytesを超えました")
        self.written += len(encoded)
        return self.stream.write(value)
    def flush(self):
        return self.stream.flush()

def validate_query(sql, allowed_tables):
    expressions = parse(sql, read="duckdb")
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Query):
        raise ValueError("SQLは単一のSELECT文に限定されています")
    expression = expressions[0]
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    for table in expression.find_all(exp.Table):
        if table.db or table.catalog or table.name.lower() not in allowed_tables | cte_names:
            raise ValueError("許可されていないテーブル参照です: " + table.sql())
    for function in expression.find_all(exp.Func):
        if isinstance(function, (exp.Case, exp.If, exp.And, exp.Or)):
            continue
        if function.sql_name().upper() not in ALLOWED_DATA_FUNCTIONS:
            raise ValueError("許可されていないSQL関数です: " + function.sql_name())
    with_clause = expression.args.get("with_")
    if with_clause is not None and with_clause.args.get("recursive"):
        raise ValueError("再帰CTEは許可されていません")

def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)

def _read_sqlite_period(path, start, end):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as source:
        source.row_factory = sqlite3.Row
        columns = source.execute("PRAGMA table_info(filter_decision_events)").fetchall()
        if not columns:
            raise RuntimeError("SQLiteにfilter_decision_eventsテーブルがありません")
        rows = source.execute(
            "SELECT * FROM filter_decision_events "
            "WHERE substr(occurred_at, 1, 10) >= ? AND substr(occurred_at, 1, 10) <= ?",
            (start, end),
        ).fetchall()
    return [dict(row) for row in rows]

def _load_memory_tables(connection, parquet_paths, sqlite_path, start, end):
    if parquet_paths:
        connection.read_parquet(parquet_paths, hive_partitioning=True).create("minute_bars")
    else:
        connection.execute(
            "CREATE TABLE minute_bars(time VARCHAR, price DOUBLE, cumulative_volume DOUBLE, "
            "volume DOUBLE, source VARCHAR, date DATE, symbol BIGINT)"
        )

    rows = _read_sqlite_period(sqlite_path, start, end)
    if rows:
        arrow_table = pa.Table.from_pylist(rows)
        connection.from_arrow(arrow_table).create("filter_decision_events")
    else:
        connection.execute(
            "CREATE TABLE filter_decision_events(id BIGINT, event_type VARCHAR, symbol VARCHAR, "
            "execution_mode VARCHAR, occurred_at VARCHAR, reference_price DOUBLE, quantity BIGINT, "
            "atr DOUBLE, true_range DOUBLE, atr_ratio DOUBLE, atr_level VARCHAR, stop_multiplier DOUBLE, "
            "market_regime VARCHAR, realized_volatility_percent DOUBLE, vix DOUBLE, "
            "nikkei_change_percent DOUBLE, adx DOUBLE, rsi DOUBLE, rsi_normal_threshold DOUBLE, "
            "rsi_applied_threshold DOUBLE, input_json VARCHAR, lowest_price DOUBLE, highest_price DOUBLE, "
            "last_price DOUBLE, last_observed_at VARCHAR, observation_count BIGINT, status VARCHAR, "
            "finalized_at VARCHAR, price_change_percent DOUBLE, hypothetical_pnl_before_cost DOUBLE, "
            "outcome VARCHAR, created_at VARCHAR)"
        )

def _query(connection, sql, table):
    validate_query(sql, {table})
    cursor = connection.execute(sql)
    names = [column[0] for column in cursor.description or []]
    rows = cursor.fetchmany(MAX_QUERY_ROWS + 1)
    if len(rows) > MAX_QUERY_ROWS:
        raise ValueError("1クエリの結果上限1000行を超えました。SQL側で集約・制限してください")
    return [{name: _json_value(value) for name, value in zip(names, row)} for row in rows]

def main():
    payload = json.loads(sys.stdin.read())
    connection = duckdb.connect(
        database=":memory:", config={"memory_limit": "512MB", "threads": "1"}
    )
    _load_memory_tables(
        connection, payload["parquet_paths"], payload["sqlite_path"],
        payload["start_date"], payload["end_date"],
    )
    connection.execute("SET enable_external_access=false")
    namespace = {
        "__builtins__": {**SAFE_BUILTINS, "print": print},
        "query_parquet": lambda sql: _query(connection, sql, "minute_bars"),
        "query_filter_events": lambda sql: _query(connection, sql, "filter_decision_events"),
    }
    sys.stdout = LimitedWriter(sys.__stdout__)
    try:
        exec(compile(payload["code"], "<generated-analysis>", "exec"), namespace, namespace)
        sys.stdout.flush()
    except BaseException:
        traceback.print_exc(file=sys.__stderr__)
        raise
    finally:
        connection.close()

if __name__ == "__main__":
    try:
        main()
    except BaseException:
        traceback.print_exc(file=sys.__stderr__)
        raise
'''


class StrategyAnalysisCodeSandbox:
    """期間限定の読取専用データと制限Python構文で分析コードを子プロセス実行する。"""

    def __init__(
        self,
        parquet_directory: Path,
        sqlite_path: Path,
        python_executable: str | None = None,
        timeout_seconds: float = 60.0,
    ):
        self.parquet_directory = Path(parquet_directory)
        self.sqlite_path = Path(sqlite_path)
        self.python_executable = python_executable or sys.executable
        self.timeout_seconds = timeout_seconds

    def parquet_paths(self, start: date, end: date) -> list[Path]:
        paths = []
        current = start
        while current <= end:
            paths.extend(sorted(self.parquet_directory.glob(f"symbol=*/date={current.isoformat()}/data.parquet")))
            current += timedelta(days=1)
        return paths

    def describe_data_sources(self, start: date, end: date) -> dict[str, Any]:
        paths = self.parquet_paths(start, end)
        parquet_schema: list[dict[str, str]] = []
        parquet_bytes = 0
        parquet_hashes = []
        if paths:
            connection = duckdb.connect(database=":memory:")
            try:
                path_sql = ", ".join(
                    "'" + str(path.resolve()).replace("'", "''") + "'" for path in paths
                )
                rows = connection.execute(
                    f"DESCRIBE SELECT * FROM read_parquet([{path_sql}], hive_partitioning=true)"
                ).fetchall()
                parquet_schema = [{"column": row[0], "type": row[1]} for row in rows]
            finally:
                connection.close()
            for path in paths:
                parquet_bytes += path.stat().st_size
                digest = _sha256_file(path)
                parquet_hashes.append({"path": str(path.resolve()), "sha256": digest})
        else:
            parquet_schema = [
                {"column": "time", "type": "VARCHAR"},
                {"column": "price", "type": "DOUBLE"},
                {"column": "cumulative_volume", "type": "DOUBLE"},
                {"column": "volume", "type": "DOUBLE"},
                {"column": "source", "type": "VARCHAR"},
                {"column": "date", "type": "DATE"},
                {"column": "symbol", "type": "VARCHAR"},
            ]

        sqlite_schema = []
        if self.sqlite_path.is_file():
            uri = self.sqlite_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                columns = connection.execute("PRAGMA table_info(filter_decision_events)").fetchall()
                sqlite_schema = [
                    {"column": str(column[1]), "type": str(column[2])}
                    for column in columns
                ]
            sqlite_hashes = {}
            for source_path in (self.sqlite_path, Path(f"{self.sqlite_path}-wal")):
                if source_path.is_file():
                    sqlite_hashes[str(source_path.resolve())] = _sha256_file(source_path)
        else:
            sqlite_hashes = {}

        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "parquet": {
                "path_pattern": "data/minute_bars_parquet/symbol=<symbol>/date=<YYYY-MM-DD>/data.parquet",
                "table": "minute_bars",
                "columns": parquet_schema,
                "schema_source": "対象期間の実ParquetをDuckDB DESCRIBEで確認。期間内にファイルがない場合はrepository定義を使用",
                "files_in_period": len(paths),
                "bytes_in_period": parquet_bytes,
                "sha256_by_file": parquet_hashes,
            },
            "sqlite": {
                "path": str(self.sqlite_path.resolve()),
                "table": "filter_decision_events",
                "columns": sqlite_schema,
                "schema_source": "読み取り専用接続のPRAGMA table_infoで確認",
                "sha256_files": sqlite_hashes,
                "period_scope": "occurred_atの日付が対象期間内の行のみ",
            },
            "query_api": {
                "query_parquet(sql)": "DuckDB。minute_barsのみ、SELECTのみ、最大1000行",
                "query_filter_events(sql)": "DuckDBメモリ表。filter_decision_eventsのみ、SELECTのみ、最大1000行",
            },
        }

    def compare_source_fingerprints(self, schema: dict[str, Any]) -> dict[str, Any]:
        mismatches = []
        parquet = schema.get("parquet", {})
        for source in parquet.get("sha256_by_file", []):
            path = Path(source["path"])
            if not path.is_file() or _sha256_file(path) != source["sha256"]:
                mismatches.append(str(path))
        sqlite = schema.get("sqlite", {})
        for source_text, expected_hash in sqlite.get("sha256_files", {}).items():
            path = Path(source_text)
            if not path.is_file() or _sha256_file(path) != expected_hash:
                mismatches.append(str(path))
        return {"matches": not mismatches, "mismatches": mismatches}

    def run(self, code: str, start: date, end: date) -> StrategyAnalysisExecutionResult:
        started = time.perf_counter()
        try:
            validate_generated_code(code)
        except SandboxValidationError as exc:
            return StrategyAnalysisExecutionResult(
                stdout="", stderr=str(exc), elapsed_seconds=time.perf_counter() - started,
                timed_out=False, return_code=None, status="rejected",
            )
        if start > end:
            return StrategyAnalysisExecutionResult(
                stdout="", stderr="分析期間の開始日が終了日より後です", elapsed_seconds=0.0,
                timed_out=False, return_code=None, status="rejected",
            )
        if not self.sqlite_path.is_file():
            return StrategyAnalysisExecutionResult(
                stdout="", stderr=f"SQLiteデータベースがありません: {self.sqlite_path}",
                elapsed_seconds=time.perf_counter() - started, timed_out=False,
                return_code=None, status="failed",
            )

        worker_path = Path(__file__).resolve()
        payload = {
            "code": code,
            "parquet_paths": [str(path.resolve()) for path in self.parquet_paths(start, end)],
            "sqlite_path": str(self.sqlite_path.resolve()),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        environment = {
            key: value
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if (value := os.environ.get(key)) is not None
        }
        with tempfile.TemporaryDirectory(prefix="strategy-analysis-sandbox-") as working_directory:
            process = subprocess.Popen(
                [self.python_executable, "-I", "-B", str(worker_path)],
                cwd=working_directory,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                stdout, stderr = process.communicate(
                    json.dumps(payload, ensure_ascii=False), timeout=self.timeout_seconds
                )
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                timed_out = True

        elapsed = time.perf_counter() - started
        if timed_out:
            status = "timed_out"
        elif process.returncode == 0:
            status = "succeeded"
        else:
            status = "failed"
        return StrategyAnalysisExecutionResult(
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=elapsed,
            timed_out=timed_out,
            return_code=process.returncode,
            status=status,
        )


if __name__ == "__main__":
    exec(_WORKER_SOURCE, {"__name__": "__main__"})