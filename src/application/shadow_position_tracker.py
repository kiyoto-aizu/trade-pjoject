"""売買を行わず保有ポジションのシャドウ観測値を組み立てる。

decision_records.detail_json schema (prices/reference in yen, quantities in shares, times as
local ISO timestamps, elapsed values in trading minutes): event_type, position_key, symbol,
buy_at, buy_price, buy_quantity, checkpoint_minutes, observed_at, observed_elapsed_minutes,
shadow_sell_price, highest_price, lowest_price, observation_count, reference_value,
reference_kind, range_ratio (unitless fraction; 0.2 means 20%), data_quality, and
capacity_blocked_candidates. Unavailable numeric values are omitted by DecisionJournalRepository
and must be interpreted as null alongside data_quality. A linked settlement record stores
settled_at, settled_price, settled_quantity, settled_quantity_total, settlement_reason, and
the same position_key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import math

CHECKPOINT_MINUTES = (15, 30, 45, 60)
LAST_OBSERVATION_TIME = time(15, 20)
MINIMUM_OBSERVATIONS = 5


def elapsed_trading_minutes(start: datetime, end: datetime) -> int:
    """Return elapsed market minutes, excluding lunch and clamping at 15:20."""
    if end <= start:
        return 0
    day_start = datetime.combine(start.date(), time(9, 0))
    morning_end = datetime.combine(start.date(), time(11, 30))
    afternoon_start = datetime.combine(start.date(), time(12, 30))
    day_end = datetime.combine(start.date(), LAST_OBSERVATION_TIME)
    segments = ((day_start, morning_end), (afternoon_start, day_end))
    total = 0.0
    for segment_start, segment_end in segments:
        left = max(start, segment_start)
        right = min(end, segment_end)
        if right > left:
            total += (right - left).total_seconds() / 60.0
    return int(total)


def resolve_reference(atr: float | None, previous_close: float | None) -> tuple[float | None, str]:
    if atr is not None and math.isfinite(float(atr)) and float(atr) > 0:
        return float(atr), "daily_atr"
    if previous_close is not None and math.isfinite(float(previous_close)) and float(previous_close) > 0:
        return float(previous_close) * 0.005, "previous_close_0.5_percent"
    return None, "unavailable"


@dataclass
class ShadowPosition:
    position_key: str
    symbol: str
    buy_at: datetime
    buy_price: float
    buy_quantity: int
    reference_value: float | None
    reference_kind: str
    history_incomplete: bool = False
    highest_price: float | None = None
    lowest_price: float | None = None
    observation_count: int = 0
    completed_checkpoints: set[int] = field(default_factory=set)
    data_quality: set[str] = field(default_factory=set)
    capacity_blocked_candidates: dict[str, dict] = field(default_factory=dict)
    settled_quantity: int = 0

    def __post_init__(self) -> None:
        if not self.history_incomplete:
            self.highest_price = self.buy_price
            self.lowest_price = self.buy_price
            self.observation_count = max(self.observation_count, 1)
        else:
            self.highest_price = None
            self.lowest_price = None
            self.data_quality.add("history_missing_after_restart")
        if self.reference_value is None:
            self.data_quality.add("reference_unavailable")

    def add_blocked_candidate(self, symbol: str, occurred_at: datetime) -> None:
        if occurred_at < self.buy_at:
            return
        item = self.capacity_blocked_candidates.get(symbol)
        if item is None:
            self.capacity_blocked_candidates[symbol] = {
                "count": 1, "first_at": occurred_at.isoformat(), "last_at": occurred_at.isoformat(),
            }
            return
        item["count"] += 1
        item["last_at"] = occurred_at.isoformat()

    def observe(self, observed_at: datetime, price: float | None, quality: str | None = None) -> list[dict]:
        if quality:
            self.data_quality.add(quality)
        valid_price = None
        if price is None:
            self.data_quality.add("price_missing")
        else:
            try:
                candidate = float(price)
                if math.isfinite(candidate) and candidate > 0:
                    valid_price = candidate
                else:
                    self.data_quality.add("price_zero_or_invalid")
            except (TypeError, ValueError, OverflowError):
                self.data_quality.add("price_zero_or_invalid")

        if valid_price is not None:
            self.observation_count += 1
            if not self.history_incomplete:
                self.highest_price = max(self.highest_price, valid_price)
                self.lowest_price = min(self.lowest_price, valid_price)

        if observed_at.time() >= LAST_OBSERVATION_TIME:
            return []
        elapsed = elapsed_trading_minutes(self.buy_at, observed_at)
        due = [minutes for minutes in CHECKPOINT_MINUTES
               if minutes not in self.completed_checkpoints and elapsed >= minutes]
        rows = []
        for checkpoint in due:
            self.completed_checkpoints.add(checkpoint)
            flags = set(self.data_quality)
            if elapsed > checkpoint:
                flags.add("checkpoint_observed_late")
            if self.observation_count < MINIMUM_OBSERVATIONS:
                flags.add("few_observations")
            if valid_price is None:
                flags.add("checkpoint_price_unavailable")
            range_ratio = None
            if (not self.history_incomplete and self.highest_price is not None
                    and self.lowest_price is not None and self.reference_value is not None):
                range_ratio = (self.highest_price - self.lowest_price) / self.reference_value
            rows.append({
                "event_type": "shadow_position_checkpoint",
                "position_key": self.position_key,
                "symbol": self.symbol,
                "buy_at": self.buy_at.isoformat(),
                "buy_price": self.buy_price,
                "buy_quantity": self.buy_quantity,
                "checkpoint_minutes": checkpoint,
                "observed_at": observed_at.isoformat(),
                "observed_elapsed_minutes": elapsed,
                "shadow_sell_price": valid_price,
                "highest_price": self.highest_price,
                "lowest_price": self.lowest_price,
                "observation_count": self.observation_count,
                "reference_value": self.reference_value,
                "reference_kind": self.reference_kind,
                "range_ratio": range_ratio,
                "data_quality": sorted(flags),
                "capacity_blocked_candidates": {
                    symbol: dict(value) for symbol, value in self.capacity_blocked_candidates.items()
                    if datetime.fromisoformat(value["last_at"]) <= observed_at
                },
            })
            self.data_quality.clear()
            if self.history_incomplete:
                self.data_quality.add("history_missing_after_restart")
            if self.reference_value is None:
                self.data_quality.add("reference_unavailable")
        return rows

    def settle(self, settled_at: datetime, settled_price: float, settled_quantity: int,
               settlement_reason: str) -> dict:
        try:
            price = float(settled_price)
            if not math.isfinite(price) or price <= 0:
                self.data_quality.add("settlement_price_zero_or_invalid")
        except (TypeError, ValueError, OverflowError):
            price = 0.0
            self.data_quality.add("settlement_price_zero_or_invalid")
        self.settled_quantity += max(int(settled_quantity), 0)
        return {
            "event_type": "shadow_position_settlement",
            "position_key": self.position_key,
            "symbol": self.symbol,
            "settled_at": settled_at.isoformat(),
            "settled_price": price,
            "settled_quantity": settled_quantity,
            "settled_quantity_total": self.settled_quantity,
            "settlement_reason": settlement_reason,
            "data_quality": sorted(self.data_quality),
        }