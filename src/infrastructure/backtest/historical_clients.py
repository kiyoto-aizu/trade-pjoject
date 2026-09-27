"""過去データでTradingUseCaseを実行するための疑似クライアント。"""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from src.config import config
from src.domain.market_volatility import MarketDailyBar
from src.domain.models import FilteringResult, MinuteBar
from src.domain.volatility import DailyBar
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository

JST = timezone(timedelta(hours=9))


def _as_jst_naive(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is not None:
        return parsed.astimezone(JST).replace(tzinfo=None)
    return parsed


class HistoricalClock:
    """run()のループ1周ごとにsleep経由で進む営業日時計。"""

    def __init__(self, trading_day: date, timestamps: list[datetime]):
        normalized = [_as_jst_naive(timestamp) for timestamp in timestamps]
        if not normalized:
            raise ValueError("timestampsは1件以上必要です")
        if any(timestamp.date() != trading_day for timestamp in normalized):
            raise ValueError("timestampsはすべてtrading_dayと同じ日付で指定してください")
        if normalized != sorted(set(normalized)):
            raise ValueError("timestampsは重複のない昇順で指定してください")

        self.trading_day = trading_day
        self._timestamps = normalized
        market_close = datetime.combine(
            trading_day,
            time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE),
        ) + timedelta(seconds=1)
        if self._timestamps[-1] < market_close:
            self._timestamps.append(market_close)
        self._index = 0

    def now(self) -> datetime:
        """現在のシミュレーション時刻を返す。呼び出しでは時刻を進めない。"""
        return self._timestamps[self._index]

    def current_date(self) -> date:
        return self.trading_day

    def advance(self, _seconds: float = 0) -> None:
        """sleep注入用。1回につき1ティック進み、終端で停止する。"""
        self._index = min(self._index + 1, len(self._timestamps) - 1)


@dataclass(frozen=True)
class DatedDailyBar:
    """日付を保持した銘柄または指数の日足OHLC。"""

    date: date
    high: float
    low: float
    close: float
    open: float | None = None


class HistoricalBoardClient:
    """時計時点までに観測済みの分足価格を板価格として返す。"""

    def __init__(
        self,
        minute_bars: dict[str, list[MinuteBar]],
        clock: HistoricalClock,
    ):
        self._clock = clock
        self._series: dict[str, tuple[list[datetime], list[float]]] = {}
        for symbol, bars in minute_bars.items():
            ordered = sorted(bars, key=lambda bar: _as_jst_naive(bar.time))
            times = [_as_jst_naive(bar.time) for bar in ordered]
            self._series[symbol] = (times, [float(bar.price) for bar in ordered])

    def get_current_board(self, token: str, symbol: str) -> dict | None:
        del token
        series = self._series.get(symbol)
        if series is None:
            return None
        times, prices = series
        index = bisect_right(times, self._clock.now()) - 1
        if index < 0:
            return None
        return {"current_price": prices[index]}


class HistoricalMarketDataClient:
    """シミュレーション日より前の確定日足を供給する。"""

    def __init__(
        self,
        daily_bars: dict[str, list[DatedDailyBar]],
        clock: HistoricalClock,
    ):
        self._daily_bars = {
            symbol: sorted(bars, key=lambda bar: bar.date)
            for symbol, bars in daily_bars.items()
        }
        self._clock = clock

    def _bars_before_today(self, symbol: str) -> list[DatedDailyBar]:
        return [
            bar for bar in self._daily_bars.get(symbol, [])
            if bar.date < self._clock.current_date()
        ]

    def get_yahoo_daily_bars(self, symbol: str) -> list[DailyBar]:
        return [
            DailyBar(high=bar.high, low=bar.low, close=bar.close)
            for bar in self._bars_before_today(symbol)
        ]

    def get_yahoo_daily_closes(self, symbol: str) -> list[float]:
        return [bar.close for bar in self._bars_before_today(symbol)]

    def get_daily_ohlc(
        self,
        symbol: str,
        range_: str = "max",
    ) -> list[MarketDailyBar]:
        del range_
        return [
            MarketDailyBar(
                date=bar.date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
            )
            for bar in self._bars_before_today(symbol)
            if bar.open is not None
        ]


class HistoricalFilteringResultRepository(FilteringResultRepository):
    def __init__(self, directory: Path, clock: HistoricalClock):
        super().__init__(directory)
        self._clock = clock

    def load_latest(self) -> FilteringResult | None:
        return self.load_for_date(self._clock.current_date())


class NoOpDailyAnalyzer:
    def analyze(self, daily_summary: dict) -> None:
        del daily_summary
        return None


class NoOpFilterDecisionRepository:
    """判定イベントを保存せず、SQLite接続も作らない。"""

    def record_event(
        self,
        event_type: str,
        symbol: str,
        occurred_at: datetime,
        reference_price: float,
        quantity: int,
        inputs: Mapping[str, object] | None = None,
        execution_mode: str = "paper",
    ) -> None:
        del event_type, symbol, occurred_at, reference_price, quantity, inputs, execution_mode
        return None

    def update_open_event_observations(
        self,
        observed_at: datetime,
        prices_by_symbol: Mapping[str, float],
        execution_mode: str | None = None,
    ) -> int:
        del observed_at, prices_by_symbol, execution_mode
        return 0

    def finalize_due_events(
        self,
        as_of: datetime | date,
        observation_days: int,
        execution_mode: str | None = None,
    ) -> list[dict]:
        del as_of, observation_days, execution_mode
        return []

    def load_summaries(
        self,
        start: date | None = None,
        end: date | None = None,
        execution_mode: str | None = None,
        finalized_only: bool = False,
    ) -> list[dict]:
        del start, end, execution_mode, finalized_only
        return []