from datetime import date, datetime

from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository


def test_repository_records_and_aggregates_observations_across_instances(tmp_path):
    database_path = tmp_path / "filter_decisions.sqlite3"
    repository = FilterDecisionRepository(database_path)
    event_id = repository.record_event(
        "ATR_DANGER_SKIP", "7203", datetime(2026, 9, 11, 14, 30), 100.0, 300,
        {"atr": 5.0, "atr_ratio": 2.4}, "paper",
    )

    restarted_repository = FilterDecisionRepository(database_path)
    assert [event["id"] for event in restarted_repository.load_open_events("paper")] == [event_id]
    assert restarted_repository.update_open_event_observations(
        datetime(2026, 9, 14, 10, 0), {"7203": 96.0}, "paper"
    ) == 1
    assert restarted_repository.update_open_event_observations(
        datetime(2026, 9, 14, 14, 0), {"7203": 103.0}, "paper"
    ) == 1

    event = restarted_repository.load_open_events("paper")[0]
    assert (event["lowest_price"], event["highest_price"], event["last_price"], event["observation_count"]) == (96.0, 103.0, 103.0, 3)
    assert event["inputs"] == {"atr": 5.0, "atr_ratio": 2.4}


def test_repository_finalizes_after_five_business_days_and_stops_observing(tmp_path):
    repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")
    event_id = repository.record_event(
        "MARKET_REGIME_DANGER_SKIP", "7203", datetime(2026, 9, 11, 14, 30), 100.0, 100,
        {"market_regime": "DANGER"}, "paper",
    )
    repository.update_observation(event_id, datetime(2026, 9, 14, 10, 0), 94.0)

    assert repository.finalize_due_events(date(2026, 9, 17), 5) == []
    finalized = repository.finalize_due_events(date(2026, 9, 18), 5)

    assert finalized[0]["outcome"] == "損失回避の可能性"
    assert finalized[0]["hypothetical_pnl_before_cost"] == -600.0
    assert repository.load_open_events() == []
    assert repository.update_observation(event_id, datetime(2026, 9, 21, 10, 0), 90.0) is False


def test_repository_uses_event_specific_outcomes_and_filters_finalized_summaries(tmp_path):
    repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")
    stop_id = repository.record_event(
        "ATR_STOP_EXIT", "7203", datetime(2026, 9, 1, 14, 30), 100.0, 100, execution_mode="backtest"
    )
    relief_id = repository.record_event(
        "ADX_TREND_RELIEF", "8306", datetime(2026, 9, 1, 14, 30), 100.0, 100, execution_mode="backtest"
    )
    repository.update_observation(stop_id, datetime(2026, 9, 2, 14, 30), 95.0)
    repository.update_observation(relief_id, datetime(2026, 9, 2, 14, 30), 105.0)
    repository.finalize_due_events(date(2026, 9, 8), 5)

    summaries = repository.load_summaries(date(2026, 9, 8), date(2026, 9, 8), "backtest", finalized_only=True)
    assert {(item["event_type"], item["outcome"]) for item in summaries} == {
        ("ATR_STOP_EXIT", "下落回避の可能性"),
        ("ADX_TREND_RELIEF", "緩和取引が有利だった可能性"),
    }
    assert repository.summarize_finalized_events(
        date(2026, 9, 8), date(2026, 9, 8), "backtest"
    ) == {
        "count": 2,
        "by_event_type": {
            "ATR_STOP_EXIT": {"count": 1, "outcomes": {"下落回避の可能性": 1}},
            "ADX_TREND_RELIEF": {"count": 1, "outcomes": {"緩和取引が有利だった可能性": 1}},
        },
    }