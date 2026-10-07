import json
from datetime import date, timedelta

from src.application.price_band_trend_check import (
    PRICE_BANDS,
    load_price_band_trends,
    price_band_monthly_lines,
    price_band_rate_line,
    price_band_symbols_for_update,
    run_price_band_checks,
)
from src.application.analysis_notification import period_notification_lines
from src.application.trend_check_usecase import TrendCheckPaths
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository


def _paths(tmp_path):
    data_dir = tmp_path / "data"
    return TrendCheckPaths(
        filtering_dir=data_dir / "filtering",
        screening_dir=data_dir / "screening",
        diagnostics_dir=data_dir / "filtering_diagnostics",
        daily_cache_dir=data_dir / "cache" / "yahoo_daily",
        minute_bar_dir=data_dir / "minute_bars",
        daily_report_dir=data_dir / "reports" / "daily",
        order_history_file=data_dir / "trading" / "order_history.json",
        decision_database=data_dir / "state" / "decisions.sqlite3",
    )


def _write_selection(paths, price_band, trade_date, symbols):
    directory = (
        paths.filtering_dir
        if price_band == 270
        else paths.filtering_dir.parent / "filtering_price_bands" / str(price_band)
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{trade_date}.json").write_text(
        json.dumps(
            {"date": trade_date, "generated_at": f"{trade_date}T16:00:00", "symbols": symbols}
        ),
        encoding="utf-8",
    )


def _write_daily_history(paths, symbol, trade_date, base=100):
    paths.daily_cache_dir.mkdir(parents=True, exist_ok=True)
    target = date.fromisoformat(trade_date)
    bars = {}
    for offset in range(14, 0, -1):
        day = (target - timedelta(days=offset)).isoformat()
        bars[day] = {
            "open": base,
            "high": base + 1,
            "low": base - 1,
            "close": base,
            "volume": 1000,
        }
    bars[trade_date] = {
        "open": base,
        "high": base + 4,
        "low": base,
        "close": base + 4,
        "volume": 1000,
    }
    (paths.daily_cache_dir / f"{symbol}.json").write_text(
        json.dumps(bars), encoding="utf-8"
    )


def test_price_band_checks_use_only_selected_symbols_and_save_separate_results(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-06"
    _write_daily_history(paths, "AAA", trade_date)
    _write_daily_history(paths, "EXPENSIVE", trade_date, base=400)
    selections = {270: ["AAA"], 450: ["AAA", "EXPENSIVE"], 900: ["EXPENSIVE"]}
    for price_band in PRICE_BANDS:
        _write_selection(paths, price_band, trade_date, selections[price_band])
        diagnostic_dir = (
            paths.diagnostics_dir
            if price_band == 270
            else paths.filtering_dir.parent
            / "filtering_price_bands"
            / str(price_band)
            / "diagnostics"
        )
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        (diagnostic_dir / f"{trade_date}_090000.json").write_text(
            json.dumps({"candidates": [{"symbol": "NOT_SELECTED"}]}),
            encoding="utf-8",
        )

    result = run_price_band_checks(
        date.fromisoformat(trade_date),
        paths,
        cache_update_ok=True,
        source_database=tmp_path / "no_standard_results.sqlite3",
    )

    assert result is not None
    assert result["overall_judged"] == 4
    for price_band, slots, expected_move, expected_buyable in (
        (270, 3, 4.0, 1.0),
        (450, 2, 2.5, 0.5),
        (900, 1, 1.0, 0.0),
    ):
        item = result["bands"][str(price_band)]
        assert item["trend_rate"] == 1.0
        assert item["average_move_pct"] == expected_move
        assert item["slot_count"] == slots
        assert item["buyable_rate"] == expected_buyable
        assert item["estimate"] == expected_move * slots
        repository = TrendCheckRepository(
            tmp_path
            / "data"
            / "analysis"
            / "price_band_trend_check"
            / "databases"
            / f"{price_band}.sqlite3"
        )
        rows = repository.load_rows("v1", trade_date, trade_date)
        assert [row["symbol"] for row in rows] == sorted(selections[price_band])

    assert "270円 1/1件 (100.0%)・参考値" in price_band_rate_line(result)
    monthly_lines = price_band_monthly_lines(result)
    assert any("買える枠" in line for line in monthly_lines)
    assert any("注文上限内" in line for line in monthly_lines)
    summary = {
        "daily": {
            "report_count": 0,
            "total_profit_loss": 0,
            "order_count": 0,
            "operational_summary": {
                "market_assessment_status_counts": {},
                "log_error_count": 0,
                "emergency_stop_days": 0,
            },
            "reports": [],
        },
        "backtest": {
            "exact_period_run_available": False,
            "total_pnl": None,
            "total_trades": None,
        },
        "price_band_trend_check": result,
    }
    weekly_lines = period_notification_lines(summary, None, "週次", None)
    assert sum(line.startswith("価格帯別（選定10銘柄）:") for line in weekly_lines) == 1
    assert not any("買える枠" in line for line in weekly_lines)
    monthly_notice = period_notification_lines(summary, None, "月次", None)
    assert any(line.startswith("|270円|") for line in monthly_notice)


def test_timeout_without_saved_filter_result_is_stored_as_missing(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-06"
    diagnostics = (
        paths.filtering_dir.parent
        / "filtering_price_bands"
        / "900"
        / "diagnostics"
    )
    diagnostics.mkdir(parents=True)
    (diagnostics / f"{trade_date}_090000.json").write_text(
        json.dumps(
            {
                "date": trade_date,
                "result_saved": False,
                "summary": {"timed_out": True, "stop_reason": "FILTER_TIME_LIMIT"},
            }
        ),
        encoding="utf-8",
    )

    result = run_price_band_checks(
        date.fromisoformat(trade_date),
        paths,
        cache_update_ok=True,
        source_database=tmp_path / "no_standard_results.sqlite3",
    )

    band = result["bands"]["900"]
    assert band["missing_days"] == 1
    assert band["missing_reasons"] == {"FILTER_TIME_LIMIT": 1}
    assert band["trend_rate"] is None
    assert "900円 欠測(時間切れ)" in price_band_rate_line(result)


def test_empty_result_without_previous_screening_is_counted_as_missing(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-05"
    diagnostics = (
        paths.filtering_dir.parent
        / "filtering_price_bands"
        / "450"
        / "diagnostics"
    )
    diagnostics.mkdir(parents=True)
    (diagnostics / f"{trade_date}_093000.json").write_text(
        json.dumps(
            {
                "date": trade_date,
                "result_saved": True,
                "summary": {"input_count": 0, "selected_count": 0},
            }
        ),
        encoding="utf-8",
    )
    _write_selection(paths, 450, trade_date, [])
    status_directory = (
        paths.daily_cache_dir.parent.parent
        / "analysis"
        / "price_band_trend_check"
        / "status"
        / "450"
    )
    status_directory.mkdir(parents=True)
    (status_directory / f"{trade_date}.json").write_text(
        json.dumps(
            {
                "trade_date": trade_date,
                "price_band": 450,
                "version": "v1",
                "state": "empty_selection",
                "reason": "NO_SELECTED_SYMBOLS",
                "selected_count": 0,
                "undecidable_count": 0,
                "undecidable_reasons": {},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_price_band_trends(
        date.fromisoformat(trade_date),
        date.fromisoformat(trade_date),
        paths,
    )
    assert loaded["bands"]["450"]["missing_reasons"] == {
        "PREVIOUS_SCREENING_NOT_AVAILABLE": 1
    }

    result = run_price_band_checks(
        date.fromisoformat(trade_date),
        paths,
        cache_update_ok=True,
        source_database=tmp_path / "no_standard_results.sqlite3",
    )

    band = result["bands"]["450"]
    assert band["missing_days"] == 1
    assert band["missing_reasons"] == {"PREVIOUS_SCREENING_NOT_AVAILABLE": 1}
    assert band["empty_days"] == 0
    assert band["trend_rate"] is None
    assert "450円 欠測(前日スクリーニングなし)" in price_band_rate_line(result)


def test_ten_selected_symbols_are_the_price_band_rate_denominator(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-06"
    symbols = [f"S{i:02}" for i in range(10)]
    for symbol in symbols:
        _write_daily_history(paths, symbol, trade_date)
    _write_selection(paths, 450, trade_date, symbols)

    result = run_price_band_checks(
        date.fromisoformat(trade_date),
        paths,
        cache_update_ok=True,
        source_database=tmp_path / "no_standard_results.sqlite3",
    )

    band = result["bands"]["450"]
    assert band["selected_count"] == 10
    assert band["rate_denominator"] == 10
    assert band["trend_rate"] == 1.0
    assert band["missing_days"] == 0
    assert "450円 10/10件 (100.0%)" in price_band_rate_line(result)


def test_partial_selection_uses_selected_count_and_reports_excluded_symbol(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-06"
    symbols = ["AAA", "BBB", "NO_HISTORY"]
    for symbol in symbols[:2]:
        _write_daily_history(paths, symbol, trade_date)
    _write_selection(paths, 450, trade_date, symbols)

    result = run_price_band_checks(
        date.fromisoformat(trade_date),
        paths,
        cache_update_ok=True,
        source_database=tmp_path / "no_standard_results.sqlite3",
    )

    band = result["bands"]["450"]
    assert band["selected_count"] == 3
    assert band["judged_count"] == 2
    assert band["rate_denominator"] == 3
    assert band["trend_rate"] == 2 / 3
    assert band["evaluation_excluded_count"] == 1
    assert band["reference"] is True
    assert "450円 2/3件 (66.7%)・選定3件(10件未満)・参考値" in price_band_rate_line(result)
    assert "評価対象外1件" in price_band_rate_line(result)

def test_price_band_cache_symbols_are_deduplicated(tmp_path):
    paths = _paths(tmp_path)
    trade_date = "2026-10-07"
    _write_selection(paths, 450, trade_date, ["AAA", "BBB"])
    _write_selection(paths, 900, trade_date, ["BBB", "CCC"])

    assert price_band_symbols_for_update(paths, date.fromisoformat(trade_date)) == [
        "AAA",
        "BBB",
        "CCC",
    ]
