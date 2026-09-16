from datetime import date
from pathlib import Path

from src.domain.models import MinuteBar
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


def test_parquet_repository_retries_transient_replace_lock(tmp_path, monkeypatch):
    repository = ParquetMinuteBarRepository(tmp_path)
    target_date = date(2026, 9, 10)
    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:30:00", 99.0))

    original_replace = Path.replace
    attempts = 0

    def replace_with_transient_lock(source: Path, destination: Path):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "Access is denied", str(destination))
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace_with_transient_lock)
    monkeypatch.setattr("src.infrastructure.persistence.parquet_minute_bar_repository.time.sleep", lambda _: None)

    repository.append_bar(target_date, "7203", _bar("2026-09-10T09:31:00", 100.0))

    assert attempts == 3
    assert [bar.price for bar in repository.load_bars(target_date, "7203")] == [99.0, 100.0]

