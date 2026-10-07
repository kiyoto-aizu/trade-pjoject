"""売買0件の日の理由を1つに分類する純粋関数。記録専用で、売買判定には使わない。"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

NO_CANDIDATE = "候補なし"
CONDITION_NOT_MET = "買い条件に届かず"
MARKET_STATE_SKIP = "市場状態による見送り"
CAPACITY_OR_FUNDS = "枠・資金不足"
TRADING_HALTED = "取引停止"
OTHER = "その他"

NO_TRADE_REASONS = (
    NO_CANDIDATE,
    CONDITION_NOT_MET,
    MARKET_STATE_SKIP,
    CAPACITY_OR_FUNDS,
    TRADING_HALTED,
    OTHER,
)

_HALT_CODES = frozenset({"NEW_BUY_HALTED_STATE_SAVE_FAILURE"})
_MARKET_CODES = frozenset({"MARKET_REGIME_DANGER_SKIP"})
_DATA_CODES = frozenset({
    "INSUFFICIENT_RSI_HISTORY",
    "RSI_DATA_UNAVAILABLE",
    "BOARD_UNAVAILABLE",
    "ATR_DATA_UNAVAILABLE",
    "ATR_ENTRY_PRICE_UNAVAILABLE",
})
_CAPACITY_CODES = frozenset({
    "POSITION_LIMIT_REACHED",
    "WALLET_UNKNOWN",
    "ORDER_AMOUNT_LIMIT_EXCEEDED",
    "budget_below_one_lot",
    "buy_quantity_invalid_inputs",
    "caution_rounding_to_zero",
    "atr_quantity_adjustment_zero",
    "ORDER_QUANTITY_ZERO",
})


def classify_no_trade_reason(
    *,
    order_count: int,
    kill_switch_triggered: bool,
    emergency_stop_triggered: bool,
    candidate_count: int,
    evaluated_count: int,
    buy_signal_count: int,
    journal_reason_codes: Iterable[str],
    market_regime_skip_count: int,
    atr_danger_skip_count: int,
) -> dict | None:
    """約定が1件でもあればNone。なければ {"reason": 候補名, "detail": 補足} を返す。"""
    if order_count > 0:
        return None
    codes = Counter(journal_reason_codes)

    if kill_switch_triggered or emergency_stop_triggered or codes.keys() & _HALT_CODES:
        detail = (
            "手動緊急停止" if emergency_stop_triggered
            else "キルスイッチ" if kill_switch_triggered
            else "状態保存失敗による新規買い停止"
        )
        return {"reason": TRADING_HALTED, "detail": detail}
    if candidate_count <= 0:
        return {"reason": NO_CANDIDATE, "detail": "監視銘柄なし"}
    if market_regime_skip_count > 0 or codes.keys() & _MARKET_CODES:
        count = max(market_regime_skip_count, sum(codes[code] for code in _MARKET_CODES))
        return {"reason": MARKET_STATE_SKIP, "detail": f"市場状態による見送りの記録 {count}件"}
    capacity = sorted(codes.keys() & _CAPACITY_CODES)
    if capacity:
        return {"reason": CAPACITY_OR_FUNDS, "detail": "・".join(capacity)}
    if atr_danger_skip_count > 0:
        return {"reason": OTHER, "detail": f"ATR危険度見送り {atr_danger_skip_count}件"}
    other_codes = sorted(codes.keys() - _DATA_CODES)
    if other_codes:
        return {"reason": OTHER, "detail": "・".join(other_codes)}
    if evaluated_count > 0 and buy_signal_count == 0:
        return {
            "reason": CONDITION_NOT_MET,
            "detail": f"評価{evaluated_count}銘柄で買いシグナルなし",
        }
    if evaluated_count <= 0:
        detail = "・".join(sorted(codes)) or "買い条件の評価記録なし"
        return {"reason": OTHER, "detail": detail}
    return {"reason": OTHER, "detail": "買いシグナルありだが見送り・注文の記録なし"}


def describe_no_trade_reason(value: Mapping | None) -> str:
    """通知用の短い表記。記録がない(古いレポートなど)場合は「未記録」。"""
    if not value or not value.get("reason"):
        return "未記録"
    return str(value["reason"])
