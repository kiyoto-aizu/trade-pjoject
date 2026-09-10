"""Yahoo Financeのintraday chart APIから分足（デフォルト1分足）を取得します。"""
import logging
from datetime import datetime, timedelta, timezone

from src.api import request_handler
from src.domain.models import MinuteBar

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


def get_yahoo_intraday_bars(symbol: str, days: int = 7, interval: str = "1m") -> list[MinuteBar]:
    """
    指定銘柄の分足をYahoo Financeから取得し、MinuteBar(source="yahoo")のリストで返します。

    Yahoo Financeの制約により、1分足(interval="1m")は直近7日程度までしか遡れません。
    銘柄がどの日付の足かはMinuteBar単体ではなく、呼び出し側でtimeの日付部分から判定してください。
    """
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    params = {"interval": interval, "range": f"{days}d"}
    response = request_handler.send_get(
        url,
        params=params,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    if not response:
        return []

    try:
        result = response.get("chart", {}).get("result", [])
        if not result:
            return []
        chart_result = result[0]
        timestamps = chart_result.get("timestamp", [])
        quote = chart_result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])
        volumes = quote.get("volume", [])
    except (KeyError, TypeError, IndexError) as exc:
        logger.exception("Yahoo分足データの解析に失敗しました (%s): %s", symbol, exc)
        return []

    bars: list[MinuteBar] = []
    for index, timestamp in enumerate(timestamps):
        close = closes[index] if index < len(closes) else None
        if close is None:
            continue
        volume = volumes[index] if index < len(volumes) else None
        minute_time = datetime.fromtimestamp(timestamp, tz=JST).strftime("%Y-%m-%dT%H:%M:00")
        bars.append(MinuteBar(
            time=minute_time,
            price=float(close),
            cumulative_volume=None,
            volume=float(volume) if volume is not None else None,
            source="yahoo",
        ))
    return bars
