import json
from datetime import date, datetime
from pathlib import Path

from src.domain.models import ScreeningResult


class ScreeningResultRepository:
    def __init__(self, directory: Path):
        self.directory = directory

    def save(self, result: ScreeningResult) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{result.date}.json"
        path.write_text(json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_latest(self) -> ScreeningResult | None:
        paths = sorted(self.directory.glob("*.json"), reverse=True)
        if not paths:
            return None
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        return ScreeningResult(**data)

    def load_for_date(self, target_date: date) -> ScreeningResult | None:
        path = self.directory / f"{target_date.isoformat()}.json"
        if not path.exists():
            return None
        return ScreeningResult(**json.loads(path.read_text(encoding="utf-8")))
