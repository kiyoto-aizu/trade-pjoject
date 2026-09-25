"""ATRベースの銘柄別ボラティリティ判定と数量調整。"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence


@dataclass(frozen=True)
class DailyBar:
    """確定日足のOHLCデータ。"""

    high: float
    low: float
    close: float


class VolatilityLevel(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    DANGER = "DANGER"


@dataclass(frozen=True)
class VolatilityAssessment:
    atr: float
    latest_true_range: float
    ratio: float
    level: VolatilityLevel


def stop_loss_multiplier(
    level: VolatilityLevel,
    normal_multiplier: float,
    caution_multiplier: float,
    danger_multiplier: float,
) -> float:
    """ボラティリティレベルに対応するATR損切り倍率を返します。"""
    multipliers = {
        VolatilityLevel.NORMAL: normal_multiplier,
        VolatilityLevel.CAUTION: caution_multiplier,
        VolatilityLevel.DANGER: danger_multiplier,
    }
    multiplier = multipliers[level]
    if multiplier <= 0:
        raise ValueError("ATR損切り倍率は正数で指定してください")
    return multiplier


def resolve_atr_exit_multiplier(
    unrealized_gain: float,
    atr: float,
    level: VolatilityLevel,
    stop_normal_multiplier: float,
    stop_caution_multiplier: float,
    stop_danger_multiplier: float,
    profit_lock_normal_multiplier: float,
    profit_lock_caution_multiplier: float,
    profit_lock_danger_multiplier: float,
    profit_lock_trigger_atr_multiple: float,
) -> float:
    """含み益がATR基準で一定以上乗っているかに応じて、損切り用/利確用いずれかの倍率を返します。

    ADR-0006: 損切り用とは別に利確(トレーリングストップ)専用の倍率を用意し、
    保有中最高値がエントリー価格からATR×profit_lock_trigger_atr_multiple以上
    乖離している(=含み益がすでに一定以上乗っている)場合にのみ利確用倍率を使う。
    """
    if profit_lock_trigger_atr_multiple < 0:
        raise ValueError("利確トリガーのATR倍数は0以上で指定してください")
    is_profit_locking = atr > 0 and unrealized_gain >= atr * profit_lock_trigger_atr_multiple
    if is_profit_locking:
        return stop_loss_multiplier(
            level,
            profit_lock_normal_multiplier,
            profit_lock_caution_multiplier,
            profit_lock_danger_multiplier,
        )
    return stop_loss_multiplier(
        level,
        stop_normal_multiplier,
        stop_caution_multiplier,
        stop_danger_multiplier,
    )


def is_atr_stop_loss_triggered(
    current_price: float,
    entry_price: float,
    atr: float,
    multiplier: float,
) -> bool:
    """現在価格が(あらかじめ選択済みの倍率による)ATR決済価格以下かを判定します。"""
    if current_price <= 0 or entry_price <= 0 or atr < 0:
        raise ValueError("価格は正数、ATRは0以上で指定してください")
    if multiplier <= 0:
        raise ValueError("ATR倍率は正数で指定してください")
    return current_price <= entry_price - atr * multiplier


def _validate_bar(bar: DailyBar) -> None:
    if bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
        raise ValueError("日足の価格は正数で指定してください")
    if bar.high < bar.low:
        raise ValueError("日足の高値は安値以上で指定してください")


def calculate_true_range(bar: DailyBar, previous_close: Optional[float] = None) -> float:
    """高値・安値・前日終値からTrue Rangeを計算します。"""
    _validate_bar(bar)
    if previous_close is not None and previous_close <= 0:
        raise ValueError("前日終値は正数で指定してください")
    previous = bar.close if previous_close is None else previous_close
    return max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous))


def _true_ranges(bars: Sequence[DailyBar]) -> list[float]:
    if not bars:
        return []
    ranges = [calculate_true_range(bars[0])]
    ranges.extend(
        calculate_true_range(bar, previous_close=previous.close)
        for previous, bar in zip(bars, bars[1:])
    )
    return ranges


def calculate_atr(bars: Sequence[DailyBar], period: int = 14) -> Optional[float]:
    """True Rangeの直近period本の単純移動平均を返します。"""
    if period <= 0:
        raise ValueError("ATR期間は正数で指定してください")
    if len(bars) < period:
        return None
    ranges = _true_ranges(bars)
    return sum(ranges[-period:]) / period


def assess_volatility(
    bars: Sequence[DailyBar],
    period: int = 14,
    caution_ratio: float = 1.5,
    danger_ratio: float = 2.0,
) -> Optional[VolatilityAssessment]:
    """最新True RangeとATRを比較し、ボラティリティレベルを判定します。"""
    if caution_ratio <= 0 or danger_ratio <= caution_ratio:
        raise ValueError("ボラティリティ閾値が不正です")
    if len(bars) < period:
        return None
    ranges = _true_ranges(bars)
    atr = sum(ranges[-period:]) / period
    latest_true_range = ranges[-1]
    ratio = latest_true_range / atr if atr > 0 else 0.0
    level = (
        VolatilityLevel.DANGER if ratio >= danger_ratio
        else VolatilityLevel.CAUTION if ratio >= caution_ratio
        else VolatilityLevel.NORMAL
    )
    return VolatilityAssessment(atr, latest_true_range, ratio, level)


def adjust_quantity_for_volatility(
    base_quantity: int,
    level: VolatilityLevel,
    order_unit: int,
    caution_lot_ratio: float = 0.5,
    danger_action: str = "skip",
) -> int:
    """ボラティリティレベルに応じて売買単位の整数倍へ数量を調整します。"""
    if base_quantity < 0 or order_unit <= 0 or caution_lot_ratio <= 0 or caution_lot_ratio > 1:
        raise ValueError("数量調整パラメータが不正です")
    if level == VolatilityLevel.NORMAL:
        return base_quantity
    if level == VolatilityLevel.DANGER:
        if danger_action == "skip":
            return 0
        if danger_action == "minimum":
            return order_unit if base_quantity >= order_unit else 0
        raise ValueError("danger_action は skip または minimum を指定してください")
    if level != VolatilityLevel.CAUTION:
        raise ValueError("未知のボラティリティレベルです")
    reduced = int(base_quantity * caution_lot_ratio) // order_unit * order_unit
    return min(base_quantity, max(order_unit, reduced)) if base_quantity >= order_unit else 0