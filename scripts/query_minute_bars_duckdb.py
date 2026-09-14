"""Parquet分足をDuckDBで横断確認します。"""
import argparse
from pathlib import Path


def query(directory: Path) -> list[tuple]:
    import duckdb

    pattern = str(directory / "symbol=*" / "date=*" / "data.parquet")
    return duckdb.connect().execute(
        """
        SELECT symbol, date, COUNT(*) AS bar_count,
               MIN(time) AS first_time, MAX(time) AS last_time
        FROM read_parquet(?, hive_partitioning = true)
        GROUP BY symbol, date
        ORDER BY date, symbol
        """,
        [pattern],
    ).fetchall()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parquet分足をDuckDBで集計します")
    parser.add_argument("--directory", type=Path, default=Path("data/minute_bars_parquet"))
    args = parser.parse_args()
    for row in query(args.directory):
        print("\t".join(str(value) for value in row))