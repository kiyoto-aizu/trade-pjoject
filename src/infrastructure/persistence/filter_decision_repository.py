"""フィルタ・見送り・緩和の判定イベントをSQLiteへ永続化するリポジトリ。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping


EVENT_TYPES = frozenset({
    "ATR_DANGER_SKIP",
    "ATR_STOP_EXIT",
    "MARKET_REGIME_DANGER_SKIP",
    "MARKET_REGIME_CAUTION_RSI_FILTER",
    "ADX_TREND_RELIEF",
})


class FilterDecisionRepository:
    """判定イベントの観測値をイベント行へ集約して保存します。"""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS filter_decision_events (
                    id INTEGER PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    atr REAL,
                    true_range REAL,
                    atr_ratio REAL,
                    atr_level TEXT,
                    stop_multiplier REAL,
                    market_regime TEXT,
                    realized_volatility_percent REAL,
                    vix REAL,
                    nikkei_change_percent REAL,
                    adx REAL,
                    rsi REAL,
                    rsi_normal_threshold REAL,
                    rsi_applied_threshold REAL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    lowest_price REAL NOT NULL,
                    highest_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    observation_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'observing',
                    finalized_at TEXT,
                    price_change_percent REAL,
                    hypothetical_pnl_before_cost REAL,
                    outcome TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (event_type IN (
                        'ATR_DANGER_SKIP', 'ATR_STOP_EXIT', 'MARKET_REGIME_DANGER_SKIP',
                        'MARKET_REGIME_CAUTION_RSI_FILTER', 'ADX_TREND_RELIEF'
                    )),
                    CHECK (status IN ('observing', 'finalized'))
                );
                CREATE INDEX IF NOT EXISTS idx_filter_decision_open
                    ON filter_decision_events(status, symbol, execution_mode);
                CREATE INDEX IF NOT EXISTS idx_filter_decision_finalized
                    ON filter_decision_events(status, finalized_at, execution_mode);
            """)

    @staticmethod
    def _iso_datetime(value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _business_days_elapsed(start: date, end: date) -> int:
        """発生日の翌日から終了日までの平日数を返します。"""
        if end <= start:
            return 0
        elapsed = 0
        current = start + timedelta(days=1)
        while current <= end:
            if current.weekday() < 5:
                elapsed += 1
            current += timedelta(days=1)
        return elapsed

    def record_event(
        self,
        event_type: str,
        symbol: str,
        occurred_at: datetime,
        reference_price: float,
        quantity: int,
        inputs: Mapping[str, object] | None = None,
        execution_mode: str = "paper",
    ) -> int:
        """イベントを記録し、同日・同種別・同銘柄の未確定イベントは再利用します。"""
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未対応の判定イベント種別です: {event_type}")
        if reference_price <= 0:
            raise ValueError("reference_priceは正数を指定してください")
        if quantity <= 0:
            raise ValueError("quantityは正数を指定してください")

        values = dict(inputs or {})
        occurred_text = self._iso_datetime(occurred_at)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id FROM filter_decision_events
                WHERE event_type = ? AND symbol = ? AND execution_mode = ?
                  AND status = 'observing' AND substr(occurred_at, 1, 10) = ?
                """,
                (event_type, symbol, execution_mode, occurred_at.date().isoformat()),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO filter_decision_events (
                    event_type, symbol, execution_mode, occurred_at, reference_price, quantity,
                    atr, true_range, atr_ratio, atr_level, stop_multiplier, market_regime,
                    realized_volatility_percent, vix, nikkei_change_percent, adx, rsi,
                    rsi_normal_threshold, rsi_applied_threshold, input_json,
                    lowest_price, highest_price, last_price, last_observed_at,
                    observation_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    event_type, symbol, execution_mode, occurred_text, reference_price, quantity,
                    values.get("atr"), values.get("true_range"), values.get("atr_ratio"),
                    values.get("atr_level"), values.get("stop_multiplier"), values.get("market_regime"),
                    values.get("realized_volatility_percent"), values.get("vix"),
                    values.get("nikkei_change_percent"), values.get("adx"), values.get("rsi"),
                    values.get("rsi_normal_threshold"), values.get("rsi_applied_threshold"),
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                    reference_price, reference_price, reference_price, occurred_text, occurred_text,
                ),
            )
            return int(cursor.lastrowid)

    def load_open_events(self, execution_mode: str | None = None) -> list[dict]:
        query = "SELECT * FROM filter_decision_events WHERE status = 'observing'"
        parameters: tuple[object, ...] = ()
        if execution_mode is not None:
            query += " AND execution_mode = ?"
            parameters = (execution_mode,)
        query += " ORDER BY occurred_at, id"
        with self._connect() as connection:
            return [self._row_to_dict(row) for row in connection.execute(query, parameters)]

    def update_observation(self, event_id: int, observed_at: datetime, price: float) -> bool:
        """未確定イベントの観測集約値を更新し、対象があればTrueを返します。"""
        if price <= 0:
            raise ValueError("観測価格は正数を指定してください")
        observed_text = self._iso_datetime(observed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE filter_decision_events
                SET lowest_price = MIN(lowest_price, ?), highest_price = MAX(highest_price, ?),
                    last_price = ?, last_observed_at = ?, observation_count = observation_count + 1
                WHERE id = ? AND status = 'observing'
                """,
                (price, price, price, observed_text, event_id),
            )
            return cursor.rowcount == 1

    def update_open_event_observations(
        self,
        observed_at: datetime,
        prices_by_symbol: Mapping[str, float],
        execution_mode: str | None = None,
    ) -> int:
        """取得できた板価格で、同じ銘柄の未確定イベントをまとめて更新します。"""
        open_events = self.load_open_events(execution_mode)
        updated = 0
        for event in open_events:
            price = prices_by_symbol.get(event["symbol"])
            if price is not None and self.update_observation(event["id"], observed_at, float(price)):
                updated += 1
        return updated

    def finalize_due_events(
        self,
        as_of: datetime | date,
        observation_days: int,
        execution_mode: str | None = None,
    ) -> list[dict]:
        """指定営業日数を経過したイベントを確定し、確定済みデータを返します。"""
        if observation_days <= 0:
            raise ValueError("observation_daysは正数を指定してください")
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
        finalized_at = self._iso_datetime(as_of) if isinstance(as_of, datetime) else datetime.combine(as_of, datetime.min.time()).isoformat(timespec="seconds")
        finalized: list[dict] = []
        for event in self.load_open_events(execution_mode):
            occurred_date = datetime.fromisoformat(event["occurred_at"]).date()
            if self._business_days_elapsed(occurred_date, as_of_date) < observation_days:
                continue
            summary = self._summarize(event)
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE filter_decision_events
                    SET status = 'finalized', finalized_at = ?, price_change_percent = ?,
                        hypothetical_pnl_before_cost = ?, outcome = ?
                    WHERE id = ? AND status = 'observing'
                    """,
                    (
                        finalized_at, summary["price_change_percent"],
                        summary["hypothetical_pnl_before_cost"], summary["outcome"], event["id"],
                    ),
                )
                if cursor.rowcount == 1:
                    finalized.append({**event, **summary, "status": "finalized", "finalized_at": finalized_at})
        return finalized

    def load_summaries(
        self,
        start: date | None = None,
        end: date | None = None,
        execution_mode: str | None = None,
        finalized_only: bool = False,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if start is not None:
            clauses.append("substr(COALESCE(finalized_at, occurred_at), 1, 10) >= ?")
            parameters.append(start.isoformat())
        if end is not None:
            clauses.append("substr(COALESCE(finalized_at, occurred_at), 1, 10) <= ?")
            parameters.append(end.isoformat())
        if execution_mode is not None:
            clauses.append("execution_mode = ?")
            parameters.append(execution_mode)
        if finalized_only:
            clauses.append("status = 'finalized'")
        query = "SELECT * FROM filter_decision_events"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occurred_at, id"
        with self._connect() as connection:
            return [self._summary_from_row(row) for row in connection.execute(query, parameters)]

    def summarize_finalized_events(
        self, start: date, end: date, execution_mode: str | None = None
    ) -> dict:
        """期間内に確定したイベントを種別・結果分類ごとに集計します。"""
        summaries = self.load_summaries(start, end, execution_mode, finalized_only=True)
        by_type: dict[str, dict] = {}
        for item in summaries:
            summary = by_type.setdefault(item["event_type"], {"count": 0, "outcomes": {}})
            summary["count"] += 1
            outcome = item["outcome"]
            summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1
        return {"count": len(summaries), "by_event_type": by_type}

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["inputs"] = json.loads(result.pop("input_json"))
        return result

    @classmethod
    def _summary_from_row(cls, row: sqlite3.Row) -> dict:
        event = cls._row_to_dict(row)
        return {**event, **cls._summarize(event)}

    @staticmethod
    def _summarize(event: Mapping[str, object]) -> dict:
        reference_price = float(event["reference_price"])
        last_price = float(event["last_price"])
        quantity = int(event["quantity"])
        event_type = str(event["event_type"])
        if event_type == "ATR_STOP_EXIT":
            hypothetical_pnl = (reference_price - last_price) * quantity
            outcome = (
                "下落回避の可能性" if hypothetical_pnl > 0 else
                "早すぎる決済の可能性" if hypothetical_pnl < 0 else "売却後の値動きなし"
            )
        elif event_type == "ADX_TREND_RELIEF":
            hypothetical_pnl = (last_price - reference_price) * quantity
            outcome = (
                "緩和取引が有利だった可能性" if hypothetical_pnl > 0 else
                "緩和取引が不利だった可能性" if hypothetical_pnl < 0 else "値動きなし"
            )
        else:
            hypothetical_pnl = (last_price - reference_price) * quantity
            outcome = (
                "損失回避の可能性" if hypothetical_pnl < 0 else
                "利益取り逃しの可能性" if hypothetical_pnl > 0 else "値動きなし"
            )
        return {
            "price_change_percent": (last_price - reference_price) / reference_price * 100,
            "hypothetical_pnl_before_cost": hypothetical_pnl,
            "outcome": outcome,
        }