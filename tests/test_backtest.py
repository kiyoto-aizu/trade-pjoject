import json

import pytest

from src.application.backtest_usecase import simulate_backtest, simulate_timeseries_backtest
from src.config import config
from src.domain.rules import calculate_rsi
from src.entrypoints.run_backtest import fetch_yahoo_history, load_history, save_backtest_result
from src.infrastructure.analysis.daily_analyzer import OpenAIDailyAnalyzer
from src.entrypoints.run_monthly_analysis import build_monthly_summary


@pytest.fixture(autouse=True)
def legacy_backtest_signal_parameters(monkeypatch):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_BUY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_SELL_THRESHOLD", 0.0)


def test_calculate_rsi_uses_wilder_smoothing():
    closes = [100.0] * 16 + [101.0, 100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0, 104.0, 106.0, 105.0, 107.0, 106.0]

    rsi = calculate_rsi(closes, period=14, minimum_closes=30)

    assert rsi is not None
    assert 50.0 < rsi < 70.0


def test_fetch_yahoo_history_reads_live_response(monkeypatch):
    class DummyResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"chart":{"result":[{"indicators":{"quote":[{"close":[100.0, 101.0, null, 103.0]}]}}]}}'

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: DummyResponse())

    history = fetch_yahoo_history(["7203"], days=4)

    assert history["7203"] == [100.0, 101.0, 103.0]


def test_load_history_from_csv(tmp_path):
    csv_path = tmp_path / "history.csv"
    csv_path.write_text(
        "symbol,close\n7203,100\n7203,101\n7203,102\n",
        encoding="utf-8",
    )

    history = load_history(csv_path)

    assert history["7203"] == [100.0, 101.0, 102.0]


def test_save_backtest_result_keeps_latest_and_timestamped_archive(tmp_path):
    output_path = tmp_path / "latest_timeseries.json"
    result = {"total_pnl": 123.45, "trade_history": []}

    archive_path = save_backtest_result(output_path, result)

    assert output_path.exists()
    assert archive_path.exists()
    assert archive_path != output_path
    assert archive_path.stem.startswith("latest_timeseries_")
    assert output_path.read_text(encoding="utf-8") == archive_path.read_text(encoding="utf-8")


def test_backtest_analyzer_uses_backtest_specific_review_prompt(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "結果の評価\n- 参考評価です。"}}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.daily_analyzer.requests.post", post)
    analyzer = OpenAIDailyAnalyzer("key", "model", "https://example.test")

    result = analyzer.analyze_backtest({"総損益": 100.0, "最大ドローダウン": 50.0})

    assert result.startswith("結果の評価")
    assert "バックテスト集計" in captured["payload"]["messages"][1]["content"]


def test_build_monthly_summary_aggregates_daily_reports_and_backtests(tmp_path):
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    reports.mkdir()
    backtests.mkdir()
    (reports / "2026-09-01.json").write_text(
        json.dumps({"date": "2026-09-01", "trading_mode": "ペーパートレード", "order_count": 2, "total_profit_loss": 100, "kill_switch_triggered": False}),
        encoding="utf-8",
    )
    (reports / "2026-09-02.json").write_text(
        json.dumps({"date": "2026-09-02", "trading_mode": "ペーパートレード", "order_count": 1, "total_profit_loss": -20, "kill_switch_triggered": True}),
        encoding="utf-8",
    )
    (backtests / "latest_timeseries_20260902.json").write_text(
        json.dumps({"generated_at": "2026-09-02T17:00:00", "total_pnl": 250, "total_trades": 4}),
        encoding="utf-8",
    )

    summary = build_monthly_summary("2026-09", reports, backtests)

    assert summary["daily"]["order_count"] == 3
    assert summary["daily"]["total_profit_loss"] == 80
    assert summary["daily"]["kill_switch_days"] == 1
    assert summary["backtest"]["total_pnl"] == 250
    assert summary["backtest"]["total_trades"] == 4


def test_simulate_backtest_buys_then_sells_on_signal():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert result["total_trades"] == 2
    assert result["cash"] == 10_400.0
    assert result["final_position"] == 0
    assert result["total_pnl"] == 400.0


def test_simulate_timeseries_backtest_uses_daily_symbol_sets():
    dated_history = {
        "7203": {
            "2026-09-01": 100.0,
            "2026-09-02": 100.0,
            "2026-09-03": 100.0,
            "2026-09-04": 100.0,
            "2026-09-05": 100.0,
            "2026-09-06": 98.0,
            "2026-09-07": 102.0,
        },
    }
    daily_symbols = {
        "2026-09-06": ["7203"],
        "2026-09-07": ["7203"],
    }

    result = simulate_timeseries_backtest(
        daily_symbols,
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
    )

    assert result["total_trades"] == 2
    assert result["total_pnl"] == 400.0
    assert result["period_start"] == "2026-09-06"
    assert result["period_end"] == "2026-09-07"


def test_simulate_backtest_does_not_use_current_price_for_signal_baseline():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 100.65],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert result["total_trades"] == 2
    assert result["final_position"] == 0


def test_simulate_backtest_fees_and_position_state():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
    )

    assert result["total_trades"] == 2
    assert result["final_position"] == 0
    assert 10_000.0 < result["cash"] < 10_400.0
    assert result["total_pnl"] < 400.0


def test_simulate_backtest_reports_summary_metrics():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        fee_rate=0.001,
    )

    assert "win_rate" in result
    assert "max_drawdown" in result
    assert "profit_factor" in result
    assert 0.0 <= result["win_rate"] <= 1.0
    assert result["max_drawdown"] >= 0.0
    assert result["profit_factor"] >= 0.0


def test_simulate_backtest_tracks_trade_history_with_holding_days():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 95.0, 103.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert "trade_history" in result
    assert len(result["trade_history"]) == 1
    assert result["trade_history"][0]["symbol"] == "7203"
    assert result["trade_history"][0]["realized_pnl"] == 800.0
    assert result["trade_history"][0]["holding_days"] == 1


def test_simulate_backtest_summarizes_symbol_and_holding_period_results():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 110.0, 90.0, 90.0, 90.0, 120.0, 120.0],
        "7204": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 98.0, 102.0],
    }

    result = simulate_backtest(["7203", "7204"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert "summary_by_symbol" in result
    assert "holding_bucket_summary" in result
    assert any(entry["symbol"] == "7203" for entry in result["summary_by_symbol"])
    assert any(entry["bucket"] == "1-3" for entry in result["holding_bucket_summary"])
    assert sum(entry["total_realized_pnl"] for entry in result["summary_by_symbol"]) > 0


def test_simulate_backtest_tracks_daily_summary_for_review():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 95.0, 103.0, 95.0, 103.0, 105.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=True,
    )

    assert "daily_summary" in result
    assert len(result["daily_summary"]) >= 1
    assert all("day_index" in entry for entry in result["daily_summary"])
    assert all("total_realized_pnl" in entry for entry in result["daily_summary"])


def test_simulate_backtest_supports_tunable_signal_thresholds():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 97.1, 103.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        buy_threshold_ratio=0.98,
        sell_threshold_ratio=1.03,
        noise_band_ratio=0.02,
    )

    assert result["total_trades"] == 0
    assert result["cash"] == 10_000.0
    assert result["final_position"] == 0


def test_simulate_backtest_applies_stop_loss_to_limit_downside():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 96.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        stop_loss_ratio=0.02,
    )

    assert result["total_trades"] >= 1
    assert result["final_position"] == 0
    assert min(entry["realized_pnl"] for entry in result["trade_history"]) < 0


def test_simulate_backtest_requires_directional_momentum_for_signal():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        trend_strength_ratio=0.05,
    )

    assert result["total_trades"] == 0
    assert result["final_position"] == 0


def test_simulate_backtest_closes_positions_at_end_of_day_for_day_trade_mode():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 103.0, 105.0],
    }

    result = simulate_backtest(
        ["7203"],
        history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=True,
    )

    assert result["final_position"] == 0
    assert len(result["trade_history"]) >= 1
    assert all(entry["holding_days"] <= 1 for entry in result["trade_history"])
