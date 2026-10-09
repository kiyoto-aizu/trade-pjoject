"""分足から09:00〜09:30の売買代金を推定するための純粋な計算ロジック。"""

WINDOW_START = "09:00"
WINDOW_END = "09:30"  # 含まない
LAST_FIVE_START = "09:25"


def is_in_window(hhmm: str) -> bool:
    return WINDOW_START <= hhmm < WINDOW_END


def aggregate_window(bars, day: str) -> dict | None:
    """指定日の09:00〜09:30(09:30の足を含めない)を集計する。その日の窓内に足が無ければNone。"""
    day_bars = [b for b in bars if b.time[:10] == day]
    if not day_bars:
        return None
    volume = value = last_five = 0.0
    in_window = 0
    first_time = last_time = None
    for bar in sorted(day_bars, key=lambda item: item.time):
        hhmm = bar.time[11:16]
        if not is_in_window(hhmm):
            continue
        in_window += 1
        first_time = first_time or bar.time
        last_time = bar.time
        bar_volume = bar.volume or 0.0
        volume += bar_volume
        value += bar.price * bar_volume
        if hhmm >= LAST_FIVE_START:
            last_five += bar_volume
    if in_window == 0:
        return None
    return {
        "bar_count": in_window,
        "volume": volume,
        "value_estimate": value,
        "last_five_volume": last_five,
        "first_bar_time": first_time,
        "last_bar_time": last_time,
    }


def judge(window: dict | None) -> str:
    if window is None:
        return "NO_BARS"
    return "TRADED" if window["volume"] > 0 else "NO_VOLUME"


def stats(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"min": round(ordered[0], 3), "median": round(median, 3), "max": round(ordered[-1], 3),
            "count": len(ordered)}


def calibrate(estimated: dict[str, float], kabu_ratios: dict[str, float], yahoo_values: dict[str, float]) -> dict:
    """板の実倍率とYahoo推定倍率の比(推定/実)。Yahoo売買代金0の銘柄は別枠。"""
    pairs, zero_yahoo = [], []
    for symbol, kabu in kabu_ratios.items():
        if symbol not in estimated or kabu <= 0:
            continue
        if yahoo_values.get(symbol, 0) == 0:
            zero_yahoo.append(symbol)
            continue
        pairs.append({"symbol": symbol, "kabu_ratio": round(kabu, 3),
                      "estimated_ratio": round(estimated[symbol], 3),
                      "estimate_over_kabu": round(estimated[symbol] / kabu, 3)})
    return {"pairs": pairs, "stats": stats([p["estimate_over_kabu"] for p in pairs]),
            "zero_yahoo_value_symbols": zero_yahoo}


def top_symbols(ratios: dict[str, float], top_n: int = 10) -> list[str]:
    """倍率の大きい順(同率は銘柄コード順)に上位N銘柄を返す。"""
    return [symbol for symbol, _ in sorted(ratios.items(), key=lambda item: (-item[1], item[0]))[:top_n]]


def compare_with_board(estimated: dict[str, float], board_ratios: dict[str, float],
                       yahoo_values: dict[str, float], top_n: int = 10) -> dict:
    """推定倍率と板の実測倍率の比較(推定/実測の分布・上位N件の一致数)。"""
    comparison = calibrate(estimated, board_ratios, yahoo_values)
    estimated_top = top_symbols(estimated, top_n)
    board_top = top_symbols(board_ratios, top_n)
    comparison.update({
        "estimated_top": estimated_top,
        "board_top": board_top,
        "top_n": top_n,
        "top_overlap_count": len(set(estimated_top) & set(board_top)),
        "estimated_count": len(estimated),
        "board_count": len(board_ratios),
    })
    return comparison
