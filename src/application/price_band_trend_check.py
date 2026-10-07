from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from src.application.trend_check_usecase import (
    DEFAULT_DATABASE_FILE,
    LABEL_TREND,
    TrendCheckPaths,
    ensure_builtin_versions,
    evaluate_day,
    load_selected_symbols,
    summarize_rows,
)
from src.config import config
from src.domain.trend_check import JUDGED_LABELS
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.persistence.storage import write_json
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

PRICE_BANDS = (270, 450, 900)
VERSION = "v1"


def _data_directory(paths: TrendCheckPaths) -> Path:
    if paths.daily_cache_dir.name == "yahoo_daily":
        return paths.daily_cache_dir.parent.parent
    return paths.daily_cache_dir.parent


def _storage_directory(paths: TrendCheckPaths) -> Path:
    return _data_directory(paths) / "analysis" / "price_band_trend_check"


def _database_path(paths: TrendCheckPaths, price_band: int) -> Path:
    return _storage_directory(paths) / "databases" / f"{price_band}.sqlite3"


def _status_directory(paths: TrendCheckPaths, price_band: int) -> Path:
    return _storage_directory(paths) / "status" / str(price_band)


def _status_path(paths: TrendCheckPaths, price_band: int, trade_date: str) -> Path:
    return _status_directory(paths, price_band) / f"{trade_date}.json"


def _filtering_paths(paths: TrendCheckPaths, price_band: int) -> TrendCheckPaths:
    if price_band == 270:
        return paths
    filtering_directory = (
        paths.filtering_dir.parent / "filtering_price_bands" / str(price_band)
    )
    return replace(
        paths,
        filtering_dir=filtering_directory,
        diagnostics_dir=filtering_directory / "diagnostics",
    )


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _read_status(paths: TrendCheckPaths, price_band: int, trade_date: str) -> dict | None:
    return _read_json(_status_path(paths, price_band, trade_date))


def _write_status(
    paths: TrendCheckPaths, price_band: int, trade_date: str, status: dict
) -> None:
    status_path = _status_path(paths, price_band, trade_date)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(status_path, status)


def _result_dates(paths: TrendCheckPaths, price_band: int, through: str) -> set[str]:
    band_paths = _filtering_paths(paths, price_band)
    dates = {
        path.stem
        for path in band_paths.filtering_dir.glob("????-??-??.json")
        if path.stem <= through
    }
    dates.update(
        path.name[:10]
        for path in band_paths.diagnostics_dir.glob("????-??-??_*.json")
        if path.name[:10] <= through
    )
    return dates


def _previous_screening_file(price_band: int, trade_date: str) -> Path:
    previous_day = date.fromisoformat(trade_date) - timedelta(days=1)
    while not is_trading_day(previous_day):
        previous_day -= timedelta(days=1)
    return (
        config.SCREENING_PRICE_BAND_RESULT_ROOT
        / str(price_band)
        / f"{previous_day.isoformat()}.json"
    )


def _latest_diagnostic(
    paths: TrendCheckPaths, price_band: int, trade_date: str
) -> dict | None:
    band_paths = _filtering_paths(paths, price_band)
    diagnostics = sorted(band_paths.diagnostics_dir.glob(f"{trade_date}_*.json"))
    for diagnostic_path in reversed(diagnostics):
        diagnostic = _read_json(diagnostic_path)
        if diagnostic is not None:
            return diagnostic
    return None


def _missing_reason(paths: TrendCheckPaths, price_band: int, trade_date: str) -> str:
    diagnostic = _latest_diagnostic(paths, price_band, trade_date)
    summary = (diagnostic or {}).get("summary") or {}
    if summary.get("stop_reason") == "FILTER_TIME_LIMIT":
        return "FILTER_TIME_LIMIT"
    if price_band != 270 and not _previous_screening_file(
        price_band, trade_date
    ).exists():
        return "PREVIOUS_SCREENING_NOT_AVAILABLE"
    return "FILTERING_RESULT_NOT_SAVED"


def _empty_selection_is_missing_screening(
    paths: TrendCheckPaths, price_band: int, trade_date: str
) -> bool:
    diagnostic = _latest_diagnostic(paths, price_band, trade_date)
    summary = (diagnostic or {}).get("summary") or {}
    return (
        summary.get("input_count") == 0
        and not _previous_screening_file(price_band, trade_date).exists()
    )


def price_band_symbols_for_update(
    paths: TrendCheckPaths, trade_date: date
) -> list[str]:
    """Include pending 450/900 selections so missed cache updates catch up."""
    through = trade_date.isoformat()
    symbols: set[str] = set()
    for price_band in (450, 900):
        band_paths = _filtering_paths(paths, price_band)
        for day in _result_dates(paths, price_band, through):
            status = _read_status(paths, price_band, day)
            if status and status.get("state") == "evaluated" and day != through:
                continue
            selected = load_selected_symbols(band_paths, day)
            if selected:
                symbols.update(selected)
    return sorted(symbols)


def _standard_rows_for_day(
    paths: TrendCheckPaths,
    trade_date: str,
    source_database: Path,
    selected_symbols: list[str],
) -> tuple[list[dict], dict] | None:
    if not source_database.exists():
        return None
    source = TrendCheckRepository(source_database)
    summaries = source.load_summaries(VERSION, trade_date, trade_date)
    summary = next((item for item in summaries if item["trade_date"] == trade_date), None)
    if summary is None:
        return None
    selected_rows = [
        row
        for row in source.load_rows(VERSION, trade_date, trade_date)
        if row["trade_date"] == trade_date and row["in_selected"]
    ]
    if {row["symbol"] for row in selected_rows} != set(selected_symbols):
        return None
    if not selected_rows and summary["selected_count"]:
        return None
    return selected_rows, summarize_rows(
        trade_date,
        VERSION,
        "selected_price_band",
        summary["regime"],
        selected_rows,
    )


def _store_missing_result(
    repository: TrendCheckRepository,
    trade_date: str,
) -> None:
    summary = summarize_rows(trade_date, VERSION, "selected_price_band", None, [])
    repository.replace_day(trade_date, VERSION, [], summary)


def _evaluate_price_band_day(
    paths: TrendCheckPaths,
    price_band: int,
    trade_date: str,
    *,
    source_database: Path,
) -> dict:
    band_paths = _filtering_paths(paths, price_band)
    selected = load_selected_symbols(band_paths, trade_date)
    if selected is None:
        reason = _missing_reason(paths, price_band, trade_date)
        repository = TrendCheckRepository(_database_path(paths, price_band))
        ensure_builtin_versions(repository, trade_date)
        _store_missing_result(repository, trade_date)
        status = {
            "trade_date": trade_date,
            "price_band": price_band,
            "version": VERSION,
            "state": "selection_missing",
            "reason": reason,
            "selected_count": None,
            "undecidable_count": 0,
            "undecidable_reasons": {},
        }
        _write_status(paths, price_band, trade_date, status)
        return status

    if not selected:
        if price_band != 270 and _empty_selection_is_missing_screening(
            paths, price_band, trade_date
        ):
            reason = "PREVIOUS_SCREENING_NOT_AVAILABLE"
            repository = TrendCheckRepository(_database_path(paths, price_band))
            ensure_builtin_versions(repository, trade_date)
            _store_missing_result(repository, trade_date)
            status = {
                "trade_date": trade_date,
                "price_band": price_band,
                "version": VERSION,
                "state": "selection_missing",
                "reason": reason,
                "selected_count": None,
                "undecidable_count": 0,
                "undecidable_reasons": {},
            }
            _write_status(paths, price_band, trade_date, status)
            return status
        repository = TrendCheckRepository(_database_path(paths, price_band))
        ensure_builtin_versions(repository, trade_date)
        _store_missing_result(repository, trade_date)
        status = {
            "trade_date": trade_date,
            "price_band": price_band,
            "version": VERSION,
            "state": "empty_selection",
            "reason": "NO_SELECTED_SYMBOLS",
            "selected_count": 0,
            "undecidable_count": 0,
            "undecidable_reasons": {},
        }
        _write_status(paths, price_band, trade_date, status)
        return status

    result = None
    if price_band == 270:
        result = _standard_rows_for_day(
            paths,
            trade_date,
            source_database,
            selected,
        )
    if result is None:
        repository = TrendCheckRepository(_database_path(paths, price_band))
        ensure_builtin_versions(repository, trade_date)
        registered = repository.get_version(VERSION)
        if registered is None:
            raise ValueError(f"Price-band trend-check version {VERSION} is not registered")
        result = evaluate_day(
            trade_date,
            registered["definition"],
            band_paths,
            selected_only=True,
        )
    if result is None:
        reason = "SELECTION_NOT_AVAILABLE"
        repository = TrendCheckRepository(_database_path(paths, price_band))
        ensure_builtin_versions(repository, trade_date)
        _store_missing_result(repository, trade_date)
        status = {
            "trade_date": trade_date,
            "price_band": price_band,
            "version": VERSION,
            "state": "selection_missing",
            "reason": reason,
            "selected_count": None,
            "undecidable_count": 0,
            "undecidable_reasons": {},
        }
        _write_status(paths, price_band, trade_date, status)
        return status

    rows, summary = result
    repository = TrendCheckRepository(_database_path(paths, price_band))
    ensure_builtin_versions(repository, trade_date)
    repository.replace_day(trade_date, VERSION, rows, summary)
    reasons = Counter(
        row.get("undecidable_reason") or "UNSPECIFIED"
        for row in rows
        if row["label"] not in JUDGED_LABELS
    )
    status = {
        "trade_date": trade_date,
        "price_band": price_band,
        "version": VERSION,
        "state": "evaluated",
        "reason": None,
        "selected_count": summary["selected_count"],
        "undecidable_count": summary["selected_undecidable"],
        "undecidable_reasons": dict(reasons),
    }
    _write_status(paths, price_band, trade_date, status)
    return status


def run_price_band_checks(
    trade_date: date,
    paths: TrendCheckPaths,
    *,
    cache_update_ok: bool,
    source_database: Path = DEFAULT_DATABASE_FILE,
) -> dict | None:
    through = trade_date.isoformat()
    if not cache_update_ok:
        for price_band in PRICE_BANDS:
            existing = _read_status(paths, price_band, through)
            if not existing:
                _write_status(
                    paths,
                    price_band,
                    through,
                    {
                        "trade_date": through,
                        "price_band": price_band,
                        "version": VERSION,
                        "state": "cache_update_failed",
                        "reason": "DAILY_CACHE_UPDATE_FAILED",
                        "selected_count": None,
                        "undecidable_count": 0,
                        "undecidable_reasons": {},
                    },
                )
        return None

    for price_band in PRICE_BANDS:
        dates = _result_dates(paths, price_band, through)
        dates.add(through)
        for day in sorted(dates):
            existing = _read_status(paths, price_band, day)
            if day != through and existing:
                if existing.get("state") == "evaluated":
                    continue
                if existing.get("state") == "empty_selection":
                    if price_band == 270 or not _empty_selection_is_missing_screening(
                        paths, price_band, day
                    ):
                        continue
            _evaluate_price_band_day(
                paths,
                price_band,
                day,
                source_database=source_database,
            )
    return load_price_band_trends(trade_date, trade_date, paths)


def _status_files(paths: TrendCheckPaths, price_band: int, start: str, end: str) -> dict[str, dict]:
    statuses = {}
    for path in _status_directory(paths, price_band).glob("????-??-??.json"):
        if start <= path.stem <= end:
            status = _read_json(path)
            if status is not None:
                if (
                    price_band != 270
                    and status.get("state") == "empty_selection"
                    and _empty_selection_is_missing_screening(
                        paths, price_band, path.stem
                    )
                ):
                    status = {
                        **status,
                        "state": "selection_missing",
                        "reason": "PREVIOUS_SCREENING_NOT_AVAILABLE",
                        "selected_count": None,
                    }
                statuses[path.stem] = status
    return statuses


def load_price_band_trends(
    start: date,
    end: date,
    paths: TrendCheckPaths | None = None,
) -> dict:
    paths = paths or TrendCheckPaths.default()
    start_text, end_text = start.isoformat(), end.isoformat()
    collected: dict[int, dict] = {}
    overall_judged = 0

    for price_band in PRICE_BANDS:
        database_path = _database_path(paths, price_band)
        statuses = _status_files(paths, price_band, start_text, end_text)
        summaries: list[dict] = []
        rows: list[dict] = []
        if database_path.exists():
            repository = TrendCheckRepository(database_path)
            summaries = repository.load_summaries(VERSION, start_text, end_text)
            rows = repository.load_rows(VERSION, start_text, end_text)
        valid_days = {
            item["trade_date"]
            for item in summaries
            if statuses.get(item["trade_date"], {}).get("state", "evaluated") == "evaluated"
        }
        selected_summaries = [
            item for item in summaries if item["trade_date"] in valid_days
        ]
        selected_rows = [
            row for row in rows
            if row["trade_date"] in valid_days and row["in_selected"]
        ]
        selected_count = sum(int(item["selected_count"]) for item in selected_summaries)
        judged_count = sum(int(item["selected_judged"]) for item in selected_summaries)
        trend_count = sum(int(item["selected_trend"]) for item in selected_summaries)
        rate_denominator = judged_count if price_band == 270 else selected_count
        overall_judged += judged_count
        undecidable_reasons = Counter(
            row.get("undecidable_reason") or "UNSPECIFIED"
            for row in selected_rows
            if row["label"] not in JUDGED_LABELS
        )
        trend_moves = [
            float(row["open_to_close_pct"])
            for row in selected_rows
            if row["label"] == LABEL_TREND and row["open_to_close_pct"] is not None
        ]
        buyable_count = sum(
            1
            for row in selected_rows
            if row["close"] is not None
            and float(row["close"]) * config.ORDER_UNIT <= config.MAX_ORDER_AMOUNT_PER_TRADE
        )
        missing_reasons = Counter(
            str(status.get("reason") or "UNSPECIFIED")
            for status in statuses.values()
            if status.get("state") in {"selection_missing", "cache_update_failed"}
        )
        empty_days = sum(
            1 for status in statuses.values() if status.get("state") == "empty_selection"
        )
        collected[price_band] = {
            "price_band": price_band,
            "selected_count": selected_count,
            "judged_count": judged_count,
            "rate_denominator": rate_denominator,
            "trend_count": trend_count,
            "trend_rate": trend_count / rate_denominator if rate_denominator else None,
            "average_move_pct": (
                sum(trend_moves) / len(trend_moves) if trend_moves else None
            ),
            "slot_count": min(
                config.TARGET_POSITIONS,
                math.floor(config.OPERATING_CAPITAL / (price_band * config.ORDER_UNIT)),
            ),
            "buyable_count": buyable_count,
            "buyable_rate": buyable_count / selected_count if selected_count else None,
            "estimate": None,
            "reference": False,
            "missing_days": sum(missing_reasons.values()),
            "missing_reasons": dict(missing_reasons),
            "empty_days": empty_days,
            "undecidable_count": sum(
                int(status.get("undecidable_count") or 0)
                for day, status in statuses.items()
                if day in valid_days
            ),
            "evaluation_excluded_count": sum(
                1 for row in selected_rows if row["label"] not in JUDGED_LABELS
            ),
            "undecidable_reasons": dict(undecidable_reasons),
        }

    for item in collected.values():
        if item["trend_rate"] is not None and item["average_move_pct"] is not None:
            item["estimate"] = (
                item["trend_rate"] * item["average_move_pct"] * item["slot_count"]
            )
        item["reference"] = (
            item["judged_count"] < 10
            or (item["price_band"] != 270 and item["selected_count"] < 10)
            or overall_judged < 30
        )

    return {
        "start_date": start_text,
        "end_date": end_text,
        "overall_judged": overall_judged,
        "bands": {str(key): value for key, value in collected.items()},
    }


def _missing_reason_label(reason: str) -> str:
    return {
        "PREVIOUS_SCREENING_NOT_AVAILABLE": "前日スクリーニングなし",
        "FILTER_TIME_LIMIT": "時間切れ",
        "FILTERING_RESULT_NOT_SAVED": "フィルタ結果未保存",
        "DAILY_CACHE_UPDATE_FAILED": "日足更新失敗",
    }.get(reason, reason)


def _missing_text(item: dict, *, include_counts: bool) -> str:
    if not item["missing_reasons"]:
        return ""
    reasons = [
        (
            f"{_missing_reason_label(reason)}{count}日"
            if include_counts
            else _missing_reason_label(reason)
        )
        for reason, count in sorted(item["missing_reasons"].items())
    ]
    return f"欠測({'・'.join(reasons)})"


def _rate_text(item: dict, *, include_missing: bool = False) -> str:
    if item["trend_rate"] is None:
        if item["missing_days"]:
            value = (
                "欠測"
                if item["price_band"] == 270
                else _missing_text(item, include_counts=False)
            )
        elif item["selected_count"] == 0 and item["empty_days"]:
            value = "選定なし"
        elif item["selected_count"] == 0:
            value = "データなし"
        else:
            value = "判定不能"
    else:
        value = (
            f"{item['trend_count']}/{item['rate_denominator']}件 "
            f"({item['trend_rate']:.1%})"
        )
    if item["price_band"] != 270 and 0 < item["selected_count"] < 10:
        value = f"{value}・選定{item['selected_count']}件(10件未満)"
    if item["reference"] and item["trend_rate"] is not None:
        value = f"{value}・参考値"
    elif item["price_band"] == 270 and item["reference"]:
        value = f"{value}・参考値"
    if (
        include_missing
        and item["price_band"] != 270
        and item["trend_rate"] is not None
        and item["missing_days"]
    ):
        value = f"{value}・{_missing_text(item, include_counts=False)}"
    if (
        include_missing
        and item["price_band"] != 270
        and item["evaluation_excluded_count"]
    ):
        value = f"{value}・評価対象外{item['evaluation_excluded_count']}件"
    return value


def price_band_rate_line(result: dict | None) -> str:
    if result is None:
        rates = " / ".join(f"{band}円 判定不能" for band in PRICE_BANDS)
    else:
        rates = " / ".join(
            f"{band}円 {_rate_text(result['bands'][str(band)], include_missing=True)}"
            for band in PRICE_BANDS
        )
    return f"価格帯別（選定10銘柄）: {rates}"


def price_band_monthly_lines(result: dict | None) -> list[str]:
    if result is None:
        return ["価格帯別（選定10銘柄）: 該当期間の記録なし"]

    lines = [
        "価格帯別（選定10銘柄）",
        "|上限|トレンド率|平均値幅|買える枠|注文上限内|目安|",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for band in PRICE_BANDS:
        item = result["bands"][str(band)]
        move = (
            f"{item['average_move_pct']:.2f}%"
            if item["average_move_pct"] is not None
            else "—"
        )
        buyable = (
            f"{item['buyable_count']}/{item['selected_count']}件 "
            f"({item['buyable_rate']:.1%})"
            if item["buyable_rate"] is not None
            else "—"
        )
        estimate = f"{item['estimate']:.2f}" if item["estimate"] is not None else "—"
        lines.append(
            f"|{band}円|{_rate_text(item)}|{move}|{item['slot_count']}枠|"
            f"{buyable}|{estimate}|"
        )
        if item["missing_reasons"]:
            if band == 270:
                reasons = ", ".join(
                    f"{reason} {count}日"
                    for reason, count in sorted(item["missing_reasons"].items())
                )
                lines.append(f"- {band}円 欠測: {reasons}")
            else:
                lines.append(f"- {band}円 {_missing_text(item, include_counts=True)}")
        if band != 270 and item["evaluation_excluded_count"]:
            lines.append(
                f"- {band}円 評価対象外: {item['evaluation_excluded_count']}件"
            )
        if item["undecidable_reasons"]:
            reasons = ", ".join(
                f"{reason} {count}件"
                for reason, count in sorted(item["undecidable_reasons"].items())
            )
            lines.append(f"- {band}円 判定不能: {reasons}")
        if item["empty_days"]:
            lines.append(f"- {band}円 選定なし: {item['empty_days']}日")
    return lines
