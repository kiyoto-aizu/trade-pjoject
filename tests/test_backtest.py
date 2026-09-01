from src.application.backtest_usecase import simulate_backtest
from src.entrypoints.run_backtest import fetch_yahoo_history, load_history


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


def test_simulate_backtest_buys_then_sells_on_signal():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
    }

    result = simulate_backtest(["7203"], history, starting_cash=10_000.0, qty_per_trade=100)

    assert result["total_trades"] == 2
    assert result["cash"] == 10_400.0
    assert result["final_position"] == 0
    assert result["total_pnl"] == 400.0


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
        "7203": [100.0, 100.0, 100.0, 100.0, 100.0, 110.0, 90.0, 120.0],
        "7204": [100.0, 100.0, 100.0, 100.0, 100.0, 98.0, 102.0],
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


def test_simulate_backtest_closes_positions_at_end_of_day_for_day_trade_mode():
    history = {
        "7203": [100.0, 100.0, 100.0, 100.0, 98.0, 103.0, 105.0],
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
