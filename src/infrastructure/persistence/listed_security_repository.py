import csv
from datetime import date
from pathlib import Path

from src.domain.models import ListedSecurity


class ListedSecurityRepository:
    """日付付き上場銘柄マスタをCSVから読み込みます。"""

    def __init__(self, path: Path):
        self.path = path

    def load_for_date(self, target_date: date) -> list[ListedSecurity]:
        target = target_date.isoformat()
        if not self.path.exists():
            raise FileNotFoundError(f"上場銘柄マスタが見つかりません: {self.path}")

        securities = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                security = ListedSecurity(
                    symbol=str(row["symbol"]).strip(),
                    exchange_division=str(row["exchange_division"]).strip(),
                    listed_from=str(row["listed_from"]).strip(),
                    listed_to=str(row.get("listed_to") or "").strip() or None,
                )
                if security.symbol and security.is_listed_on(target):
                    securities.append(security)
        return securities