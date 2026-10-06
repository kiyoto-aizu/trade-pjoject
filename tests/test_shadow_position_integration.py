import json
from datetime import datetime

from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.domain.models import PriceLimit, TradeSignal
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.decision_journal_repository import (
    STAGE_TRADING,
    DecisionJournalRepository,
)


class _FailingJournal:
    execution_mode = config.TRADING_MODE

    def record(self, *args, **kwargs):
        raise OSError("journal unavailable")


def _run_paper_round_trip(tmp_path, tracking_enabled):
    client = PaperOrderClient(prices={"1234": 100.0}, cash=100_000.0)
    usecase = TradingUseCase(
        token="test",
        order_history_path=tmp_path / f"orders-{tracking_enabled}.json",
        kill_switch_baseline_path=tmp_path / f"baseline-{tracking_enabled}.json",
        filter_decision_repository=object(),
        decision_journal_repository=_FailingJournal() if tracking_enabled else None,
        enable_shadow_position_tracking=tracking_enabled,
        order_sender=client,
        notifier=lambda _: None,
        daily_analyzer=object(),
    )
    limit = PriceLimit(lower_band=90.0, upper_band=110.0)
    usecase._current_now = datetime.fromisoformat("2026-10-06T09:00:00")
    client.set_price("1234", 100.0)
    buy_result = client.place_market_order("test", "1234", config.OrderSide.BUY.value, 100)
    usecase._register_order(
        TradeSignal("1234", config.OrderSide.BUY, 100.0, 100), limit, buy_result,
        diagnostics={"decision_reason": "test"}, shadow_previous_close=200.0,
    )
    if tracking_enabled:
        usecase._observe_shadow_positions(
            "1234", datetime.fromisoformat("2026-10-06T09:15:00"), 101.0,
        )
    usecase._current_now = datetime.fromisoformat("2026-10-06T09:20:00")
    client.set_price("1234", 101.0)
    sell_result = client.place_market_order("test", "1234", config.OrderSide.SELL.value, 100)
    usecase._register_order(
        TradeSignal("1234", config.OrderSide.SELL, 101.0, 100), limit, sell_result,
        diagnostics={"decision_reason": "test_exit"},
    )
    return client.orders, client.get_daily_realized_pnl(), usecase


def test_tracking_failures_do_not_change_paper_order_results(tmp_path, caplog):
    off_orders, off_pnl, off_usecase = _run_paper_round_trip(tmp_path, False)
    on_orders, on_pnl, on_usecase = _run_paper_round_trip(tmp_path, True)

    assert on_orders == off_orders
    assert on_pnl == off_pnl
    assert [entry.to_dict() for entry in on_usecase.order_history] == [
        entry.to_dict() for entry in off_usecase.order_history
    ]
    assert len(on_usecase._shadow_positions) == 0
    assert sum("SHADOW_RECORD_WRITE_FAILED" in record.message for record in caplog.records) == 1


def test_tracking_is_disabled_by_default_for_replay_usecases(tmp_path):
    usecase = TradingUseCase(
        token="test",
        order_history_path=tmp_path / "orders.json",
        kill_switch_baseline_path=tmp_path / "baseline.json",
        filter_decision_repository=object(),
        order_sender=PaperOrderClient(prices={"1234": 100.0}, cash=100_000.0),
        notifier=lambda _: None,
        daily_analyzer=object(),
    )
    usecase._current_now = datetime.fromisoformat("2026-10-06T09:00:00")
    result = usecase.order_sender.place_market_order(
        "test", "1234", config.OrderSide.BUY.value, 100
    )
    usecase._register_order(
        TradeSignal("1234", config.OrderSide.BUY, 100.0, 100),
        PriceLimit(90.0, 110.0), result,
    )

    assert usecase.enable_shadow_position_tracking is False
    assert usecase._shadow_positions == {}


def test_checkpoint_and_settlement_are_saved_with_position_key(tmp_path):
    journal = DecisionJournalRepository(
        tmp_path / "decisions.sqlite3", execution_mode=config.TRADING_MODE
    )
    client = PaperOrderClient(prices={"1234": 100.0}, cash=100_000.0)
    usecase = TradingUseCase(
        token="test", order_history_path=tmp_path / "orders.json",
        kill_switch_baseline_path=tmp_path / "baseline.json",
        filter_decision_repository=object(), decision_journal_repository=journal,
        enable_shadow_position_tracking=True, order_sender=client,
        notifier=lambda _: None, daily_analyzer=object(),
    )
    limit = PriceLimit(90.0, 110.0)
    usecase._current_now = datetime.fromisoformat("2026-10-06T09:00:00")
    buy = client.place_market_order("test", "1234", config.OrderSide.BUY.value, 100)
    usecase._register_order(
        TradeSignal("1234", config.OrderSide.BUY, 100.0, 100), limit, buy,
        shadow_previous_close=200.0,
    )
    usecase._note_shadow_capacity_skip("5678", datetime.fromisoformat("2026-10-06T09:05:00"))
    usecase._note_shadow_capacity_skip("5678", datetime.fromisoformat("2026-10-06T09:06:00"))
    usecase._observe_shadow_positions(
        "1234", datetime.fromisoformat("2026-10-06T09:15:00"), 101.0,
    )
    usecase._flush_decision_journal_safely()

    records = journal.load_records("2026-10-06", STAGE_TRADING, config.TRADING_MODE)
    checkpoint = next(row for row in records if "_N15" in row["reason_code"])
    detail = json.loads(checkpoint["detail_json"])
    key = detail["position_key"]
    assert detail["buy_price"] == buy["Price"]
    assert detail["reference_value"] == 1.0
    assert detail["reference_kind"] == "previous_close_0.5_percent"
    assert detail["capacity_blocked_candidates"]["5678"]["count"] == 2

    usecase._current_now = datetime.fromisoformat("2026-10-06T09:20:00")
    client.set_price("1234", 102.0)
    sell = client.place_market_order("test", "1234", config.OrderSide.SELL.value, 100)
    usecase._register_order(
        TradeSignal("1234", config.OrderSide.SELL, 102.0, 100), limit, sell,
        diagnostics={"decision_reason": "test_exit"},
    )
    usecase._flush_decision_journal_safely()
    records = journal.load_records("2026-10-06", STAGE_TRADING, config.TRADING_MODE)
    settlement = next(row for row in records if row["reason_code"].endswith("_SETTLEMENT"))
    settled_detail = json.loads(settlement["detail_json"])
    assert settled_detail["position_key"] == key
    assert settled_detail["settled_price"] == sell["Price"]
    assert settled_detail["settlement_reason"] == "test_exit"


def test_restart_marks_lost_extrema_and_does_not_repeat_checkpoint(tmp_path):
    journal = DecisionJournalRepository(
        tmp_path / "decisions.sqlite3", execution_mode=config.TRADING_MODE
    )
    client = PaperOrderClient(prices={"1234": 100.0}, cash=100_000.0)
    original = TradingUseCase(
        token="test", order_history_path=tmp_path / "orders.json",
        kill_switch_baseline_path=tmp_path / "baseline.json",
        filter_decision_repository=object(), decision_journal_repository=journal,
        enable_shadow_position_tracking=True, order_sender=client,
        notifier=lambda _: None, daily_analyzer=object(),
    )
    original._current_now = datetime.fromisoformat("2026-10-06T09:00:00")
    buy = client.place_market_order("test", "1234", config.OrderSide.BUY.value, 100)
    original._register_order(
        TradeSignal("1234", config.OrderSide.BUY, 100.0, 100),
        PriceLimit(90.0, 110.0), buy, shadow_previous_close=200.0,
    )
    original._observe_shadow_positions(
        "1234", datetime.fromisoformat("2026-10-06T09:15:00"), 101.0,
    )
    original._flush_decision_journal_safely()

    restarted = TradingUseCase(
        token="test", order_history_path=tmp_path / "orders.json",
        kill_switch_baseline_path=tmp_path / "baseline.json",
        filter_decision_repository=object(), decision_journal_repository=journal,
        enable_shadow_position_tracking=True, order_sender=client,
        notifier=lambda _: None, daily_analyzer=object(),
    )
    restarted.order_history = original.order_history
    restarted._restore_shadow_positions(
        [{"Symbol": "1234", "Side": config.OrderSide.SELL.value, "HoldQty": 100}],
        datetime.fromisoformat("2026-10-06T09:16:00"),
    )
    position = next(iter(restarted._shadow_positions.values()))
    assert position.highest_price is None and position.lowest_price is None
    assert "history_missing_after_restart" in position.data_quality
    rows = position.observe(datetime.fromisoformat("2026-10-06T09:30:00"), 102.0)
    assert [row["checkpoint_minutes"] for row in rows] == [30]
    assert rows[0]["range_ratio"] is None
    assert "history_missing_after_restart" in rows[0]["data_quality"]