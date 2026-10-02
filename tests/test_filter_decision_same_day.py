import sqlite3
from datetime import date, datetime, time, timedelta

from src.application.backtest_usecase import simulate_timeseries_backtest
from src.config import config
from src.domain.market_regime import MarketRegime
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

DAY = datetime(2026, 9, 11, 10, 0)
END = time(15, 20)


def _event(repository, occurred=DAY, symbol="7203", event_type="ATR_DANGER_SKIP"):
    return repository.record_event(event_type, symbol, occurred, 100.0, 100, {}, "paper")


def _row(path, event_id):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute("SELECT * FROM filter_decision_events WHERE id = ?", (event_id,)).fetchone())
    finally:
        connection.close()


def test_same_day_high_low_close_updated_and_next_day_does_not_touch(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    event_id = _event(repository)
    repository.update_observation(event_id, datetime(2026, 9, 11, 11, 0), 105.0)
    repository.update_observation(event_id, datetime(2026, 9, 11, 13, 0), 97.0)
    repository.update_observation(event_id, datetime(2026, 9, 11, 15, 0), 101.0)
    repository.update_observation(event_id, datetime(2026, 9, 14, 10, 0), 130.0)

    row = _row(path, event_id)
    assert (row["same_day_high"], row["same_day_low"], row["same_day_close_price"]) == (105.0, 97.0, 101.0)
    assert row["same_day_observation_count"] == 4
    assert row["same_day_last_observed_at"] == "2026-09-11T15:00:00"
    assert (row["highest_price"], row["last_price"]) == (130.0, 130.0)


def test_same_day_finalize_survives_restart_and_keeps_multi_day_open(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    event_id = _event(repository)
    repository.update_observation(event_id, datetime(2026, 9, 11, 15, 19), 98.0)

    restarted = FilterDecisionRepository(path)
    assert restarted.finalize_same_day_events(datetime(2026, 9, 11, 12, 0), "paper", session_end=END) == []
    finalized = restarted.finalize_same_day_events(datetime(2026, 9, 14, 9, 0), "paper", session_end=END)

    assert len(finalized) == 1
    row = _row(path, event_id)
    assert row["same_day_finalized_at"] == "2026-09-14T09:00:00"
    assert row["same_day_price_change_percent"] == -2.0
    assert row["same_day_hypothetical_pnl_before_cost"] == -200.0
    assert row["same_day_outcome"] == "損失回避の可能性"
    assert row["status"] == "observing"
    assert restarted.finalize_same_day_events(datetime(2026, 9, 14, 9, 0), "paper", session_end=END) == []


def test_include_today_finalizes_at_close(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    event_id = _event(repository)
    repository.update_observation(event_id, datetime(2026, 9, 11, 15, 19), 100.0)
    assert len(repository.finalize_same_day_events(
        datetime(2026, 9, 11, 15, 20), "paper", include_today=True, session_end=END
    )) == 1
    assert _row(path, event_id)["same_day_finalized_at"] == "2026-09-11T15:20:00"


def test_data_quality_ok_few_stale_and_board_unavailable(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    ok_id = _event(repository, symbol="1001")
    for minute in range(0, 330, 1):
        repository.update_observation(ok_id, datetime(2026, 9, 11, 10, 0) + timedelta(minutes=minute), 100.0)
    few_id = _event(repository, symbol="1002")
    repository.update_observation(few_id, datetime(2026, 9, 11, 10, 1), 100.0)
    repository.update_observation(few_id, datetime(2026, 9, 11, 14, 59), 100.0)
    board_id = _event(repository, symbol="1003")
    assert repository.mark_same_day_board_unavailable("1003", datetime(2026, 9, 11, 11, 0), "paper") == 1
    assert repository.mark_same_day_board_unavailable("1003", datetime(2026, 9, 11, 11, 1), "paper") == 0

    repository.finalize_same_day_events(date(2026, 9, 12), "paper", session_end=END)

    assert _row(path, ok_id)["same_day_data_quality"] == "OK"
    few_quality = _row(path, few_id)["same_day_data_quality"]
    assert few_quality.startswith("FEW_OBSERVATIONS(3/")
    assert "STALE" not in few_quality
    board_quality = _row(path, board_id)["same_day_data_quality"]
    assert board_quality.startswith("BOARD_UNAVAILABLE")
    assert "STALE_LAST_OBSERVATION" in board_quality


def test_existing_database_is_migrated_and_legacy_rows_stay_null(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    FilterDecisionRepository(path)
    connection = sqlite3.connect(path)
    columns = [row[1] for row in connection.execute("PRAGMA table_info(filter_decision_events)")]
    for column in columns:
        if column.startswith("same_day_"):
            connection.execute(f"ALTER TABLE filter_decision_events DROP COLUMN {column}")
    connection.execute(
        "INSERT INTO filter_decision_events (event_type, symbol, execution_mode, occurred_at, reference_price, "
        "quantity, input_json, lowest_price, highest_price, last_price, last_observed_at, observation_count, "
        "status, created_at) VALUES ('ATR_DANGER_SKIP', '7203', 'paper', '2026-09-10T10:00:00', 100, 100, '{}', "
        "100, 100, 100, '2026-09-10T10:00:00', 1, 'observing', '2026-09-10T10:00:00')"
    )
    connection.commit()
    connection.close()

    repository = FilterDecisionRepository(path)
    assert repository.update_open_event_observations(datetime(2026, 9, 10, 11, 0), {"7203": 90.0}, "paper") == 1
    assert repository.finalize_same_day_events(datetime(2026, 9, 14, 9, 0), "paper") == []
    row = _row(path, 1)
    assert row["same_day_close_price"] is None and row["same_day_finalized_at"] is None
    summary = repository.load_summaries()[0]
    assert summary["evaluations"]["same_day"] is None
    assert summary["evaluations"]["multi_day"]["last_price"] == 90.0


def test_summaries_and_period_aggregation_show_both_bases(tmp_path):
    repository = FilterDecisionRepository(tmp_path / "events.sqlite3")
    event_id = _event(repository)
    repository.update_observation(event_id, datetime(2026, 9, 11, 15, 19), 98.0)
    repository.update_observation(event_id, datetime(2026, 9, 14, 10, 0), 120.0)
    repository.finalize_same_day_events(datetime(2026, 9, 11, 15, 20), "paper", include_today=True, session_end=END)

    item = repository.load_summaries()[0]
    assert item["evaluations"]["same_day"]["outcome"] == "損失回避の可能性"
    assert item["evaluations"]["multi_day"]["outcome"] == "利益取り逃しの可能性"
    assert item["outcome"] == "利益取り逃しの可能性"

    summary = repository.summarize_finalized_events(date(2026, 9, 11), date(2026, 9, 11), "paper")
    assert summary["count"] == 0
    assert summary["same_day"]["count"] == 1
    assert summary["same_day"]["by_event_type"]["ATR_DANGER_SKIP"]["pnl_before_cost_total"] == -200.0


def test_backtest_daily_events_use_simulated_close_time_and_finalize_same_day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    dated_history = {
        "7203": {
            "2026-09-01": 90.0, "2026-09-02": 90.0, "2026-09-03": 90.0, "2026-09-04": 90.0,
            "2026-09-05": 90.0, "2026-09-06": 92.0, "2026-09-07": 120.0, "2026-09-08": 98.0,
        },
    }
    regimes = {date_text: MarketRegime.DANGER for date_text in dated_history["7203"]}
    path = tmp_path / "filter_decisions.sqlite3"
    repository = FilterDecisionRepository(path)

    simulate_timeseries_backtest(
        {"2026-09-06": ["7203"], "2026-09-07": ["7203"]},
        dated_history,
        starting_cash=10_000.0,
        qty_per_trade=100,
        close_at_eod=False,
        market_regime_by_date=regimes,
        filter_decision_repository=repository,
    )

    event = repository.load_open_events("backtest")[0]
    assert not event["occurred_at"].endswith("T00:00:00")
    assert event["occurred_at"].endswith("T15:30:00")
    row = _row(path, event["id"])
    assert row["same_day_finalized_at"] is not None
    assert row["same_day_data_quality"] == "DAILY_REPLAY_NO_INTRADAY"
    assert row["status"] == "observing"
    summary = repository.summarize_finalized_events(date(2026, 9, 6), date(2026, 9, 8), "backtest")
    assert summary["same_day"]["count"] == 0
    assert summary["same_day"]["excluded_daily_replay_count"] >= 1


def test_trading_usecase_board_unavailable_and_report_shows_both_bases(tmp_path):
    from types import SimpleNamespace
    from src.application.trading_usecase import TradingUseCase

    use_case = TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / "reports",
    )
    use_case._record_filter_decision_safely("ATR_DANGER_SKIP", "3624", datetime(2026, 9, 15, 14, 55), 95.0, 300, {})
    use_case._mark_board_unavailable_safely("3624", datetime(2026, 9, 15, 15, 0))
    use_case._update_filter_decision_observations_safely(datetime(2026, 9, 15, 15, 19), {"3624": 97.0})
    use_case._finalize_same_day_filter_decisions_safely(datetime(2026, 9, 15, 15, 20), include_today=True)

    item = use_case._filter_decision_summaries("ATR_DANGER_SKIP")[0]
    same_day = item["evaluations"]["same_day"]
    assert same_day["finalized"] is True
    assert same_day["hypothetical_pnl_before_cost"] == 600.0
    assert same_day["data_quality"].startswith("BOARD_UNAVAILABLE")
    assert item["evaluations"]["multi_day"]["finalized"] is False
    assert "当日基準" in use_case._event_report_line(item)
    assert "複数営業日基準" not in use_case._event_report_line(item)


def test_board_unavailable_flag_survives_recovery_until_close_finalize(tmp_path):
    from types import SimpleNamespace
    from src.application.trading_usecase import TradingUseCase

    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'o.json', notifier=lambda m: None, daily_analyzer=SimpleNamespace(analyze=lambda s: None), daily_report_directory=tmp_path / 'r')
    use_case._record_filter_decision_safely('ATR_DANGER_SKIP', '3624', datetime(2026, 9, 15, 10, 0), 95.0, 300, {})
    use_case._mark_board_unavailable_safely('3624', datetime(2026, 9, 15, 10, 5))
    use_case._board_unavailable_symbols.add('3624')
    use_case._board_unavailable_symbols.discard('3624')
    for minute in range(10, 320):
        use_case._update_filter_decision_observations_safely(datetime(2026, 9, 15, 10, 0) + timedelta(minutes=minute), {'3624': 96.0})
    use_case._finalize_same_day_filter_decisions_safely(datetime(2026, 9, 15, 15, 20), include_today=True)

    same_day = use_case._filter_decision_summaries('ATR_DANGER_SKIP')[0]['evaluations']['same_day']
    assert same_day['finalized'] is True
    assert same_day['data_quality'] == 'BOARD_UNAVAILABLE'


FLAG = "DAILY_REPLAY_NO_INTRADAY"


def test_daily_replay_flag_is_set_at_record_time(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    flagged = repository.record_event(
        "ATR_DANGER_SKIP", "1001", datetime(2026, 9, 1, 15, 30), 100.0, 100, {}, "backtest",
        same_day_data_quality=FLAG,
    )
    plain = repository.record_event(
        "ATR_DANGER_SKIP", "1002", datetime(2026, 9, 1, 10, 0), 100.0, 100, {}, "backtest",
    )
    assert _row(path, flagged)["same_day_data_quality"] == FLAG
    assert _row(path, plain)["same_day_data_quality"] is None


def test_daily_replay_flag_survives_finalize_with_other_flags(tmp_path):
    path = tmp_path / "events.sqlite3"
    repository = FilterDecisionRepository(path)
    event_id = repository.record_event(
        "ATR_DANGER_SKIP", "1001", datetime(2026, 9, 1, 10, 0), 100.0, 100, {}, "backtest",
        same_day_data_quality=FLAG,
    )
    repository.update_observation(event_id, datetime(2026, 9, 1, 10, 1), 99.0)
    repository.finalize_same_day_events(datetime(2026, 9, 1, 15, 20), "backtest", include_today=True, session_end=END)

    flags = _row(path, event_id)["same_day_data_quality"].split(",")
    assert flags[0] == FLAG
    assert any(f.startswith("STALE_LAST_OBSERVATION") for f in flags)
    assert any(f.startswith("FEW_OBSERVATIONS") for f in flags)


def test_daily_replay_events_excluded_from_same_day_summary_only(tmp_path):
    repository = FilterDecisionRepository(tmp_path / "events.sqlite3")
    day = datetime(2026, 9, 1, 10, 0)
    repository.record_event("ATR_DANGER_SKIP", "1001", day, 100.0, 100, {}, "backtest", same_day_data_quality=FLAG)
    normal = repository.record_event(
        "ATR_DANGER_SKIP", "1002", datetime(2026, 9, 1, 15, 17), 100.0, 100, {}, "backtest"
    )
    flagged = repository.record_event("ATR_STOP_EXIT", "1003", day, 100.0, 100, {}, "backtest", same_day_data_quality=FLAG)
    repository.update_observation(normal, datetime(2026, 9, 1, 15, 19), 98.0)
    repository.update_observation(flagged, datetime(2026, 9, 1, 15, 19), 90.0)
    repository.finalize_same_day_events(datetime(2026, 9, 1, 15, 20), "backtest", include_today=True, session_end=END)
    repository.finalize_due_events(date(2026, 9, 8), 5, "backtest")

    summary = repository.summarize_finalized_events(date(2026, 9, 1), date(2026, 9, 8), "backtest")
    same_day = summary["same_day"]
    assert same_day["count"] == 1
    assert same_day["excluded_daily_replay_count"] == 2
    assert set(same_day["by_event_type"]) == {"ATR_DANGER_SKIP"}
    assert same_day["by_event_type"]["ATR_DANGER_SKIP"]["count"] == 1
    assert same_day["by_event_type"]["ATR_DANGER_SKIP"]["pnl_before_cost_total"] == -200.0
    assert same_day["data_quality"] == {"OK": 1}
    assert FLAG not in same_day["data_quality"]

    assert summary["count"] == 3
    assert set(summary["by_event_type"]) == {"ATR_DANGER_SKIP", "ATR_STOP_EXIT"}
    assert summary["by_event_type"]["ATR_DANGER_SKIP"]["count"] == 2
    assert summary["by_event_type"]["ATR_STOP_EXIT"]["count"] == 1


def test_daily_report_lists_only_report_day_events_and_cumulative_summary_unchanged(tmp_path):
    import json
    from types import SimpleNamespace
    from src.application.trading_usecase import TradingUseCase

    use_case = TradingUseCase(
        token="dummy", order_history_path=tmp_path / "order_history.json", notifier=lambda m: None,
        daily_analyzer=SimpleNamespace(analyze=lambda s: None), daily_report_directory=tmp_path / "reports",
    )
    now = datetime.now().replace(hour=15, minute=25, second=0, microsecond=0)
    yesterday = now - timedelta(days=1)
    use_case._current_now = now
    for event_type, symbol, at in (
        ("ATR_DANGER_SKIP", "1001", yesterday), ("ATR_DANGER_SKIP", "1002", now.replace(hour=15, minute=17)),
        ("MARKET_REGIME_DANGER_SKIP", "2001", yesterday), ("MARKET_REGIME_DANGER_SKIP", "2002", now.replace(hour=15, minute=17)),
    ):
        use_case._record_filter_decision_safely(event_type, symbol, at, 100.0, 100, {})

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / "reports" / f"{now.date().isoformat()}.json").read_text(encoding="utf-8"))
    assert [i["symbol"] for i in report["atr_danger_skips"]] == ["1002"]
    assert [i["symbol"] for i in report["market_regime_danger_skips"]] == ["2002"]
    assert len(use_case._filter_decision_summaries("ATR_DANGER_SKIP")) == 2

    repository = use_case.filter_decision_repository
    repository.finalize_due_events(now.date() + timedelta(days=7), 5, use_case._execution_mode)
    summary = repository.summarize_finalized_events(now.date(), now.date() + timedelta(days=7), use_case._execution_mode)
    assert summary["count"] == 4
    assert summary["by_event_type"]["ATR_DANGER_SKIP"]["count"] == 2
