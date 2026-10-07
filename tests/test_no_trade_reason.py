from src.domain.no_trade_reason import (
    CAPACITY_OR_FUNDS,
    CONDITION_NOT_MET,
    MARKET_STATE_SKIP,
    NO_CANDIDATE,
    OTHER,
    TRADING_HALTED,
    classify_no_trade_reason,
    describe_no_trade_reason,
)


def _classify(**overrides):
    values = {
        "order_count": 0,
        "kill_switch_triggered": False,
        "emergency_stop_triggered": False,
        "candidate_count": 10,
        "evaluated_count": 10,
        "buy_signal_count": 0,
        "journal_reason_codes": [],
        "market_regime_skip_count": 0,
        "atr_danger_skip_count": 0,
    }
    values.update(overrides)
    return classify_no_trade_reason(**values)


def test_no_reason_when_orders_were_filled():
    assert _classify(order_count=1) is None


def test_each_reason_is_selected_from_existing_records():
    assert _classify()["reason"] == CONDITION_NOT_MET
    assert _classify(candidate_count=0)["reason"] == NO_CANDIDATE
    assert _classify(kill_switch_triggered=True)["reason"] == TRADING_HALTED
    assert _classify(emergency_stop_triggered=True)["detail"] == "手動緊急停止"
    assert _classify(journal_reason_codes=["NEW_BUY_HALTED_STATE_SAVE_FAILURE"])["reason"] == TRADING_HALTED
    assert _classify(market_regime_skip_count=2, buy_signal_count=2)["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=["MARKET_REGIME_DANGER_SKIP"])["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=["POSITION_LIMIT_REACHED"])["reason"] == CAPACITY_OR_FUNDS
    assert _classify(journal_reason_codes=["WALLET_UNKNOWN"])["reason"] == CAPACITY_OR_FUNDS
    assert _classify(atr_danger_skip_count=1, buy_signal_count=1)["reason"] == OTHER


def test_priority_halt_then_candidates_then_market_then_capacity():
    codes = ["POSITION_LIMIT_REACHED", "MARKET_REGIME_DANGER_SKIP"]
    assert _classify(journal_reason_codes=codes)["reason"] == MARKET_STATE_SKIP
    assert _classify(journal_reason_codes=codes, kill_switch_triggered=True)["reason"] == TRADING_HALTED
    assert _classify(journal_reason_codes=codes, candidate_count=0)["reason"] == NO_CANDIDATE


def test_data_unavailable_does_not_hide_condition_not_met_but_is_other_without_evaluation():
    assert _classify(journal_reason_codes=["BOARD_UNAVAILABLE"])["reason"] == CONDITION_NOT_MET
    unevaluated = _classify(evaluated_count=0, journal_reason_codes=["BOARD_UNAVAILABLE"])
    assert unevaluated == {"reason": OTHER, "detail": "BOARD_UNAVAILABLE"}
    assert _classify(buy_signal_count=1)["reason"] == OTHER


def test_describe_marks_missing_record_as_unrecorded():
    assert describe_no_trade_reason(None) == "未記録"
    assert describe_no_trade_reason({"reason": CONDITION_NOT_MET, "detail": "x"}) == CONDITION_NOT_MET