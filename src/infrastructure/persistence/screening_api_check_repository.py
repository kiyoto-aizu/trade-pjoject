import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

from src.domain.models import Regulation

logger = logging.getLogger(__name__)


class ScreeningApiCheckRepository:
    """市場・規制APIの銘柄別結果を日付単位で保存し、同日中は再利用します。"""

    def __init__(self, directory: Path, target_date: date, exchange_repository, regulation_repository):
        self.directory = Path(directory)
        self.target_date = target_date
        self.exchange_repository = exchange_repository
        self.regulation_repository = regulation_repository
        self.path = self.directory / f"{target_date.isoformat()}.json"
        self._checks = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") != self.target_date.isoformat():
                logger.warning("市場・規制チェック記録の日付不一致: %s", self.path)
                return {}
            return data.get("checks", {})
        except (OSError, ValueError, TypeError):
            logger.exception("市場・規制チェック記録を読み込めません。再取得します: %s", self.path)
            return {}

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self.target_date.isoformat(),
            "updated_at": datetime.now().isoformat(),
            "checks": self._checks,
        }
        temporary_path = self.path.with_suffix(".json.tmp")
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windowsではウイルス対策/同期ソフト等が一時的にファイルを掴むため短くリトライする
        for attempt in range(5):
            try:
                temporary_path.replace(self.path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _entry(self, symbol: str) -> dict:
        return self._checks.setdefault(str(symbol), {})

    def get_primary_exchange(self, symbol: str) -> int | None:
        entry = self._entry(symbol)
        saved = entry.get("primary_exchange")
        if saved is not None:
            return saved.get("value") if saved.get("status") == "checked" else None

        requested_at = datetime.now()
        try:
            value = self.exchange_repository.get_primary_exchange(symbol)
        except Exception as exc:
            entry["primary_exchange"] = {
                "status": "failed",
                "value": None,
                "requested_at": requested_at.isoformat(),
                "checked_at": datetime.now().isoformat(),
                "error_type": type(exc).__name__,
            }
            self._save()
            raise

        entry["primary_exchange"] = {
            "status": "checked" if value is not None else "failed",
            "value": value,
            "requested_at": requested_at.isoformat(),
            "checked_at": datetime.now().isoformat(),
            "error_type": None if value is not None else "empty_response",
        }
        self._save()
        return value

    def get_regulation(self, symbol: str, market_code: int, target_date: date | None = None) -> Regulation:
        del target_date
        entry = self._entry(symbol)
        saved = entry.get("regulation")
        if saved is not None:
            if saved.get("status") != "checked":
                return Regulation(symbol, True, "規制情報取得失敗", market_code)
            return Regulation(
                symbol,
                bool(saved["is_restricted"]),
                str(saved.get("reason", "")),
                int(saved.get("primary_exchange", market_code)),
            )

        requested_at = datetime.now()
        try:
            result = self.regulation_repository.get_regulation(symbol, market_code)
        except Exception as exc:
            entry["regulation"] = {
                "status": "failed",
                "requested_at": requested_at.isoformat(),
                "checked_at": datetime.now().isoformat(),
                "error_type": type(exc).__name__,
            }
            self._save()
            raise

        failed = result.reason == "規制情報取得失敗"
        entry["regulation"] = {
            "status": "failed" if failed else "checked",
            "requested_at": requested_at.isoformat(),
            "checked_at": datetime.now().isoformat(),
            "is_restricted": bool(result.is_restricted),
            "reason": result.reason,
            "primary_exchange": market_code,
            "error_type": "empty_response" if failed else None,
        }
        self._save()
        return result

    def status_for(self, symbol: str) -> str:
        entry = self._checks.get(str(symbol), {})
        exchange_status = entry.get("primary_exchange", {}).get("status")
        regulation_status = entry.get("regulation", {}).get("status")
        if exchange_status == "failed":
            return "unconfirmed_market"
        if regulation_status == "failed":
            return "unconfirmed_regulation"
        if exchange_status == "checked" and regulation_status == "checked":
            return "checked"
        return "unconfirmed"