"""バックテスト用Yahoo日足取得のディスクキャッシュ。

確定済みの過去日付データを再利用し、必要範囲が欠けている銘柄だけを再取得する。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.domain.volatility import DailyBar
from src.infrastructure.market_data.yahoo_backtest_history_client import fetch_yahoo_dated_ohlc


def _cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol}.json"


def _load_cache_with_status(cache_dir: Path, symbol: str) -> tuple[dict[str, DailyBar], bool]:
    """キャッシュと、全日付にvolumeキーがあるか(旧形式でないか)を返す。値の有無ではなくキーで判定する。"""
    path = _cache_path(cache_dir, symbol)
    if not path.exists():
        return {}, True
    raw = json.loads(path.read_text(encoding="utf-8"))
    has_volume_key = all("volume" in values for values in raw.values())
    cache = {
        target_date: DailyBar(
            high=float(values["high"]),
            low=float(values["low"]),
            close=float(values["close"]),
            open=float(values["open"]) if values.get("open") is not None else None,
            volume=float(values["volume"]) if values.get("volume") is not None else None,
        )
        for target_date, values in raw.items()
    }
    return cache, has_volume_key


def _load_cache(cache_dir: Path, symbol: str) -> dict[str, DailyBar]:
    return _load_cache_with_status(cache_dir, symbol)[0]


def _save_cache(cache_dir: Path, symbol: str, history: dict[str, DailyBar]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        target_date: {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for target_date, bar in sorted(history.items())
    }
    _cache_path(cache_dir, symbol).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_yahoo_dated_ohlc_cached(
    symbols: list[str],
    days: int,
    cache_dir: Path,
    earliest_needed_date: date,
    latest_needed_date: date | None = None,
) -> dict[str, dict[str, DailyBar]]:
    """必要期間を含むキャッシュを返し、不足銘柄のみYahooから取得する。"""
    result: dict[str, dict[str, DailyBar]] = {}
    symbols_to_fetch: list[str] = []
    cached_by_symbol: dict[str, dict[str, DailyBar]] = {}
    earliest_text = earliest_needed_date.isoformat()
    latest_text = latest_needed_date.isoformat() if latest_needed_date else None

    for raw_symbol in symbols:
        symbol = str(raw_symbol)
        cached, has_volume_key = _load_cache_with_status(cache_dir, symbol)
        cached_by_symbol[symbol] = cached
        cached_dates = cached.keys()
        covers_earliest = bool(cached) and min(cached_dates) <= earliest_text
        covers_latest = latest_text is None or (bool(cached) and max(cached_dates) >= latest_text)
        has_required_ohlc = not symbol.startswith("^") or all(
            bar.open is not None for bar in cached.values()
        )
        if covers_earliest and covers_latest and has_required_ohlc and has_volume_key:
            result[symbol] = cached
        else:
            symbols_to_fetch.append(symbol)

    if symbols_to_fetch:
        fetched = fetch_yahoo_dated_ohlc(symbols_to_fetch, days=days)
        for symbol in symbols_to_fetch:
            fresh_history = fetched.get(symbol, {})
            merged = {**cached_by_symbol[symbol], **fresh_history}
            if fresh_history:
                _save_cache(cache_dir, symbol, merged)
            if merged:
                result[symbol] = merged

    return result