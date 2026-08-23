import json
from datetime import date
from pathlib import Path

from src.domain.models import FilteringResult


class FilteringResultRepository:
    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, result: FilteringResult) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{result.date}.json"
        path.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_latest(self) -> FilteringResult | None:
        paths = sorted(self.directory.glob("*.json"), reverse=True)
        if not paths:
            return None
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        return FilteringResult(**data)

    def load_for_date(self, target_date: date) -> FilteringResult | None:
        path = self.directory / f"{target_date.isoformat()}.json"
        if not path.exists():
            return None
        return FilteringResult(**json.loads(path.read_text(encoding="utf-8")))
