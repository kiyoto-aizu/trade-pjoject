"""起動時の売買設定値の検証。

値は属性として渡されたオブジェクトから読むだけで、config.pyはimportしない。
秘密値は対象外とし、項目名・現在値・許容範囲のみを扱う。
"""
import math
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass(frozen=True)
class ConfigViolation:
    name: str
    value: Any
    expected: str
    hint: str

    def format(self) -> str:
        return f"{self.name}={self.value!r} | 許容範囲: {self.expected} | 対処: {self.hint}"


class ConfigValidationError(Exception):
    """起動時の設定値検証で違反が見つかった場合の例外。全違反を保持する。"""

    def __init__(self, violations: List[ConfigViolation]):
        self.violations = list(violations)
        lines = [f"設定値の違反が{len(self.violations)}件あります。"]
        lines.extend(f"- {v.format()}" for v in self.violations)
        super().__init__("\n".join(lines))


_MISSING = object()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value == int(value)


def _collect(settings: Any) -> List[ConfigViolation]:
    violations: List[ConfigViolation] = []
    valid = {}

    def get(name: str) -> Any:
        return getattr(settings, name, _MISSING)

    def fail(name: str, value: Any, expected: str, hint: Optional[str] = None) -> None:
        shown = "未定義" if value is _MISSING else value
        violations.append(ConfigViolation(name, shown, expected, hint or f"環境変数{name}を修正してください。"))

    def check(name: str, expected: str, predicate, integer: bool = False) -> None:
        value = get(name)
        ok = (_is_integer(value) if integer else _is_number(value)) and predicate(value)
        if ok:
            valid[name] = value
        else:
            fail(name, value, expected)

    check("OPERATING_CAPITAL", "0より大きい", lambda v: v > 0)
    check("MAX_ORDER_AMOUNT_PER_TRADE", "0より大きい", lambda v: v > 0)
    if "OPERATING_CAPITAL" in valid and "MAX_ORDER_AMOUNT_PER_TRADE" in valid:
        if valid["MAX_ORDER_AMOUNT_PER_TRADE"] > valid["OPERATING_CAPITAL"]:
            fail(
                "MAX_ORDER_AMOUNT_PER_TRADE",
                valid["MAX_ORDER_AMOUNT_PER_TRADE"],
                f"OPERATING_CAPITAL({valid['OPERATING_CAPITAL']!r})以下",
                "1回の注文上限を運用資金以下にするか、OPERATING_CAPITALを引き上げてください。",
            )
    check("TARGET_POSITIONS", "1以上の整数", lambda v: v >= 1, integer=True)
    check("MAX_ORDER_COUNT_PER_DAY", "1以上の整数", lambda v: v >= 1, integer=True)
    check("DAILY_LOSS_LIMIT_RATIO", "0より大きく1以下", lambda v: 0 < v <= 1)
    check("API_SOFT_LIMIT", "0より大きい", lambda v: v > 0)

    check("RSI_PERIOD", "2以上の整数", lambda v: v >= 2, integer=True)
    if "RSI_PERIOD" in valid:
        period = valid["RSI_PERIOD"]
        check("RSI_MINIMUM_CLOSES", f"RSI_PERIOD({period!r})より大きい整数", lambda v: v > period, integer=True)
    else:
        check("RSI_MINIMUM_CLOSES", "RSI_PERIODより大きい整数", lambda v: True, integer=True)
    for name in ("RSI_ENTRY_THRESHOLD", "RSI_EXIT_THRESHOLD", "RSI_ENTRY_THRESHOLD_CAUTION"):
        check(name, "0〜100", lambda v: 0 <= v <= 100)
    if all(n in valid for n in ("RSI_ENTRY_THRESHOLD", "RSI_EXIT_THRESHOLD", "RSI_ENTRY_THRESHOLD_CAUTION")):
        exit_t = valid["RSI_EXIT_THRESHOLD"]
        entry = valid["RSI_ENTRY_THRESHOLD"]
        caution = valid["RSI_ENTRY_THRESHOLD_CAUTION"]
        if not exit_t < entry:
            fail("RSI_EXIT_THRESHOLD", exit_t, f"RSI_ENTRY_THRESHOLD({entry!r})より小さい",
                 "売り閾値は買い閾値より小さくしてください。")
        if not entry <= caution:
            fail("RSI_ENTRY_THRESHOLD_CAUTION", caution, f"RSI_ENTRY_THRESHOLD({entry!r})以上",
                 "CAUTION時の買い閾値は通常時の買い閾値以上にしてください。")

    thresholds = get("MARKET_REGIME_THRESHOLDS")
    for label, caution_attr, danger_attr in (
        ("MARKET_REGIME_REALIZED_VOL", "realized_vol_caution", "realized_vol_danger"),
        ("MARKET_REGIME_VIX", "vix_caution", "vix_danger"),
    ):
        caution = getattr(thresholds, caution_attr, _MISSING)
        danger = getattr(thresholds, danger_attr, _MISSING)
        if not (_is_number(caution) and _is_number(danger)):
            fail(f"{label}_CAUTION/_DANGER", (caution, danger), "有限な数値", f"{label}_CAUTIONと_DANGERを修正してください。")
        elif not caution < danger:
            fail(f"{label}_CAUTION/_DANGER", (caution, danger), "CAUTION < DANGER",
                 f"{label}_DANGERをCAUTIONより大きくしてください。")
    nikkei = getattr(thresholds, "nikkei_change_upgrade", _MISSING)
    if not (_is_number(nikkei) and nikkei > 0):
        fail("MARKET_REGIME_NIKKEI_CHANGE_UPGRADE", nikkei, "0より大きい")

    check("MARKET_LIQUIDATION_HOUR", "0〜23の整数", lambda v: 0 <= v <= 23, integer=True)
    check("MARKET_LIQUIDATION_MINUTE", "0〜59の整数", lambda v: 0 <= v <= 59, integer=True)

    for name in (
        "BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD",
        "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD",
        "LIQUIDATION_POSITIONS_FETCH_RETRIES",
    ):
        check(name, "1以上の整数", lambda v: v >= 1, integer=True)
    for name in (
        "KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS",
        "KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS",
        "API_REQUEST_INTERVAL_SECONDS",
        "LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS",
    ):
        check(name, "0以上", lambda v: v >= 0)

    return violations


def find_config_violations(settings: Any) -> List[ConfigViolation]:
    """違反を全件リストで返す。違反がなければ空リスト。"""
    return _collect(settings)


def validate_startup_config(settings: Any) -> List[ConfigViolation]:
    """違反が1件でもあれば、全件をまとめたConfigValidationErrorを送出する。違反なしなら空リストを返す。"""
    violations = _collect(settings)
    if violations:
        raise ConfigValidationError(violations)
    return violations
