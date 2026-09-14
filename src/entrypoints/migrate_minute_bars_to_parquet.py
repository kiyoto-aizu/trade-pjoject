"""既存のminute-bar JSONをParquetパーティションへ移行します。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

from src.domain.models import MinuteBar
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def migrate(input_directory: Path, output_directory: Path) -> dict:
    repository = ParquetMinuteBarRepository(output_directory)
    files = sorted(input_directory.glob("*/*.json"))
    counts: Counter[str] = Counter()
    dates: list[date] = []
    errors = 0
    partitions: dict[tuple[date, str], dict[str, MinuteBar]] = {}
    for path in files:
        try:
            target_date = date.fromisoformat(path.parent.name)
            symbol = path.stem
            payload = json.loads(path.read_text(encoding="utf-8"))
            partition = partitions.setdefault((target_date, symbol), {})
            for raw_bar in payload.get("bars", []):
                bar = MinuteBar(**raw_bar)
                existing = partition.get(bar.time)
                if existing is None or not (existing.source == "yahoo" and bar.source == "poll"):
                    partition[bar.time] = bar
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            errors += 1

    for (target_date, symbol), bars_by_time in sorted(partitions.items()):
        bars = list(bars_by_time.values())
        repository.replace_bars(target_date, symbol, bars)
        counts[symbol] += len(bars)
        dates.extend([target_date] * len(bars))

    return {
        "files": len(files),
        "bars": sum(counts.values()),
        "symbols": dict(sorted(counts.items())),
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="分足JSONをParquetへ移行します")
    parser.add_argument("--input", type=Path, default=Path("data/minute_bars"))
    parser.add_argument("--output", type=Path, default=Path("data/minute_bars_parquet"))
    args = parser.parse_args()
    print(json.dumps(migrate(args.input, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()