import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import json
import logging

from src.domain.models import OrderHistoryEntry
from src.domain.enums import OrderSide

logger = logging.getLogger(__name__)


class OrderHistoryManager:
    def __init__(self, path: Path):
        self.path = path
        self.orders: List[OrderHistoryEntry] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open('r', encoding='utf-8') as f:
                    data = json.load(f)
                self.orders = [OrderHistoryEntry.from_dict(d) for d in data]
            except (json.JSONDecodeError, OSError) as exc:
                logger.exception("注文履歴ファイルの読み込みに失敗しました: %s", self.path)
                raise ValueError(f"注文履歴ファイルの読み込みに失敗しました: {exc}") from exc
        else:
            self.orders = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('w', encoding='utf-8') as f:
                json.dump([o.to_dict() for o in self.orders], f, ensure_ascii=False, indent=2)
        except OSError:
            logger.exception("注文履歴の保存に失敗しました: %s", self.path)

    def register_order(self, symbol: str, side: str, price: float, qty: int) -> None:
        # normalize side to OrderSide if a raw value was passed
        side_val = side
        try:
            if not isinstance(side, OrderSide):
                side_val = OrderSide(side)
        except ValueError:
            logger.warning("不正な売買区分を受け取りました。BUY をデフォルトにします: %s", side)
            side_val = OrderSide.BUY
        entry = OrderHistoryEntry(symbol=symbol, side=side_val, price=price, qty=qty, timestamp=datetime.now().isoformat())
        self.orders.append(entry)
        self.save()

    def has_ordered_today(self, symbol: str, side: str) -> bool:
        today = datetime.now().strftime('%Y-%m-%d')
        return any(e.to_dict().get('symbol') == symbol and e.to_dict().get('side') == side and e.to_dict().get('timestamp', '').startswith(today) for e in self.orders)

    def is_recent_order(self, symbol: str, side: str, lock_seconds: int) -> bool:
        cutoff = datetime.now() - timedelta(seconds=lock_seconds)
        for e in reversed(self.orders):
            try:
                ts = datetime.fromisoformat(e.timestamp)
                if e.symbol == symbol and e.side == side and ts >= cutoff:
                    return True
            except ValueError:
                logger.debug("不正なタイムスタンプをスキップします: %s", getattr(e, 'timestamp', None))
                continue
        return False

    def todays_orders(self) -> List[Dict]:
        today = datetime.now().strftime('%Y-%m-%d')
        return [o.to_dict() for o in self.orders if o.timestamp.startswith(today)]
