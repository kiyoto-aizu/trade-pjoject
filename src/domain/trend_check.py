"""選定銘柄がトレンド追随戦略に合った動きをしたかを判定する純粋ロジック(判定のみ。売買には使わない)。

判定基準は通し番号の「版」で管理する。しきい値は仮置きで、変更時は新しい版を追加する。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional, Sequence

from src.domain.volatility import DailyBar, calculate_atr

LABEL_TREND = "トレンド(上昇)"
LABEL_REVERSE = "逆行"
LABEL_FLAT = "動かず"
LABEL_CHOPPY = "行ったり来たり"
LABEL_UNDECIDABLE = "判定不能"
JUDGED_LABELS = (LABEL_TREND, LABEL_REVERSE, LABEL_FLAT, LABEL_CHOPPY)

# 版ごとの定義(1か所管理)。レポートにも必ず表示する。
CRITERIA_V1: dict = {
    "version": "v1",
    "status": "仮置き(数値は後で調整する前提)",
    "thresholds": {
        # 始値→終値の騰落率(%)がこの値以上
        "open_to_close_min_pct": 1.0,
        # (終値-安値)/(高値-安値)がこの値以上=終値が当日レンジの上位(1-値)以内
        "close_position_min": 0.70,
        # (高値-安値)/ATRがこの値以上
        "range_to_atr_min": 0.5,
        # 逆行: 始値→終値の騰落率(%)がこの値以下
        "reverse_open_to_close_max_pct": -1.0,
    },
    "indicators": {
        "open_to_close_pct": "(終値-始値)/始値*100",
        "close_position": "(終値-安値)/(高値-安値)。高値=安値の日は0.5",
        "range_to_atr": "(高値-安値)/ATR。ATRは当日より前の確定日足から計算(期間は設定ATR_PERIOD、単純平均)",
    },
    "label_rules": [
        "トレンド(上昇): 3条件(騰落率・終値位置・レンジ/ATR)をすべて満たす",
        "逆行: トレンドでなく、始値→終値が逆行しきい値以下",
        "動かず: 上記以外で、レンジ/ATRが下限未満",
        "行ったり来たり: 上記以外(値幅はあるが方向が出なかった)",
    ],
    "auxiliary": {
        "direction_consistency": "分足の価格変化(0を除く)のうち、当日の始値→終値の方向と同じ向きの割合",
        "vwap_above_ratio": "分足のうち、その時点までの累積VWAPより価格が上にあった割合",
        "used_for_judgement": False,
    },
}

BUILTIN_CRITERIA: dict[str, dict] = {"v1": CRITERIA_V1}


def version_sort_key(version: str) -> tuple[int, str]:
    digits = "".join(ch for ch in version if ch.isdigit())
    return (int(digits) if digits else -1, version)


def definition_to_json(definition: dict) -> str:
    return json.dumps(definition, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class TrendJudgement:
    label: str
    open_to_close_pct: Optional[float] = None
    close_position: Optional[float] = None
    range_to_atr: Optional[float] = None
    atr: Optional[float] = None
    reason: Optional[str] = None  # 判定不能の理由


def calculate_atr_before(bars_by_date: dict[str, DailyBar], target_date: str, period: int) -> Optional[float]:
    """当日より前の確定日足だけでATRを計算する。"""
    history = [bars_by_date[key] for key in sorted(bars_by_date) if key < target_date]
    return calculate_atr(history, period)


def judge_day(bar: Optional[DailyBar], atr: Optional[float], definition: dict) -> TrendJudgement:
    """当日の日足とATRから版の定義に従って判定する。"""
    if bar is None:
        return TrendJudgement(LABEL_UNDECIDABLE, atr=atr, reason="日足なし")
    if bar.open is None or bar.open <= 0:
        return TrendJudgement(LABEL_UNDECIDABLE, atr=atr, reason="始値なし")
    if atr is None or atr <= 0:
        return TrendJudgement(LABEL_UNDECIDABLE, atr=atr, reason="ATR算出不可")

    limits = definition["thresholds"]
    change_pct = (bar.close - bar.open) / bar.open * 100.0
    day_range = bar.high - bar.low
    close_position = (bar.close - bar.low) / day_range if day_range > 0 else 0.5
    range_to_atr = day_range / atr

    is_trend = (
        change_pct >= limits["open_to_close_min_pct"]
        and close_position >= limits["close_position_min"]
        and range_to_atr >= limits["range_to_atr_min"]
    )
    if is_trend:
        label = LABEL_TREND
    elif change_pct <= limits["reverse_open_to_close_max_pct"]:
        label = LABEL_REVERSE
    elif range_to_atr < limits["range_to_atr_min"]:
        label = LABEL_FLAT
    else:
        label = LABEL_CHOPPY
    return TrendJudgement(label, change_pct, close_position, range_to_atr, atr)


def calculate_minute_auxiliary(bars: Sequence) -> dict[str, Optional[float]]:
    """分足(time, price, volume を持つ要素)から補助情報を計算する。判定には使わない。"""
    ordered = sorted(bars, key=lambda bar: bar.time)
    prices = [bar.price for bar in ordered if bar.price and bar.price > 0]
    result: dict[str, Optional[float]] = {"direction_consistency": None, "vwap_above_ratio": None}
    if len(prices) < 2:
        return result

    net = prices[-1] - prices[0]
    moves = [current - previous for previous, current in zip(prices, prices[1:]) if current != previous]
    if moves and net != 0:
        same = sum(1 for move in moves if (move > 0) == (net > 0))
        result["direction_consistency"] = same / len(moves)

    cumulative_value = 0.0
    cumulative_volume = 0.0
    above = 0
    counted = 0
    for bar in ordered:
        volume = bar.volume or 0.0
        cumulative_value += bar.price * volume
        cumulative_volume += volume
        if cumulative_volume > 0:
            counted += 1
            if bar.price > cumulative_value / cumulative_volume:
                above += 1
    if counted:
        result["vwap_above_ratio"] = above / counted
    return result
