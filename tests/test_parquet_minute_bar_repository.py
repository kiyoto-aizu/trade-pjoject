import json
from datetime import date

from src.domain.models import MinuteBar
from src.entrypoints.migrate_minute_bars_to_parquet import migrate
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def _bar(time: str, price: float, source: str = "poll") -> MinuteBar:
    return MinuteBar(
        time=time,
        price=price,
        cumulative_volume=None,
        volume=None,
        source=source,
    )


def test_parquet_repository_round_trip_and_partition(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path / "parquet")
    target_date = date(2026, 9, 10)

    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:31:00", 100.0))
    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:30:00", 99.0))

    assert [bar.price for bar in repository.load_bars(target_date, "7203")] == [99.0, 100.0]
    assert (tmp_path / "parquet" / "symbol=7203" / "date=2026-09-10" / "data.parquet").exists()


def test_parquet_repository_does_not_downgrade_yahoo_bar(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    target_date = date(2026, 9, 10)

    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:31:00", 101.5, "yahoo"))
    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:31:00", 999.0, "poll"))

    bars = repository.load_bars(target_date, "7203")
    assert len(bars) == 1
    assert bars[0].price == 101.5
    assert bars[0].source == "yahoo"


def test_migrate_json_minute_bars_keeps_json_and_reports_range(tmp_path):
    input_directory = tmp_path / "minute_bars"
    source_path = input_directory / "2026-09-10" / "7203.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        json.dumps({
            "date": "2026-09-10",
            "symbol": "7203",
            "bars": [
                {
                    "time": "2026-09-10T09:31:00",
                    "price": 100.0,
                    "cumulative_volume": None,
                    "volume": 10.0,
                    "source": "yahoo",
                }
            ],
        }),
        encoding="utf-8",
    )

    output_directory = tmp_path / "parquet"
    report = migrate(input_directory, output_directory)

    assert report == {
        "files": 1,
        "bars": 1,
        "symbols": {"7203": 1},
        "date_start": "2026-09-10",
        "date_end": "2026-09-10",
        "errors": 0,
    }
    assert source_path.exists()
    assert len(ParquetMinuteBarRepository(output_directory).load_bars(date(2026, 9, 10), "7203")) == 1