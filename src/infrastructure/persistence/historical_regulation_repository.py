import csv
from datetime import date
from pathlib import Path

from src.domain.models import Regulation


class HistoricalRegulationRepository:
    """日付付き規制マスタをCSVから読み込みます。"""

    def __init__(self, path: Path):
        self.path = path

    def get_regulation(
        self,
        symbol: str,
        market_code: int,
        target_date: date | None = None,
    ) -> Regulation:
        if target_date is None:
            raise ValueError("HistoricalRegulationRepositoryにはtarget_dateが必要です")
        if not self.path.exists():
            raise FileNotFoundError(f"規制マスタが見つかりません: {self.path}")

        target = target_date.isoformat()
        with self.path.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                if str(row.get("symbol", "")).strip() != str(symbol):
                    continue
                restricted_from = str(row.get("restricted_from", "")).strip()
                restricted_to = str(row.get("restricted_to", "")).strip()
                if restricted_from <= target and (not restricted_to or target <= restricted_to):
                    primary_exchange = int(row.get("primary_exchange") or market_code)
                    return Regulation(
                        symbol,
                        True,
                        str(row.get("reason") or "規制あり").strip(),
                        primary_exchange,
                    )
        return Regulation(symbol, False, "", market_code)