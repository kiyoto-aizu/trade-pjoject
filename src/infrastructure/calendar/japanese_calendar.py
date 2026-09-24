"""日本の市場稼働日カレンダーを提供します。"""
from datetime import date, datetime

import jpholiday

from src.domain.rules import is_trading_day as _is_trading_day
from src.domain.rules import is_trading_session as _is_trading_session


def is_trading_day(target_date: date) -> bool:
    """日本の祝日を考慮した市場稼働日を判定します。"""
    return _is_trading_day(target_date, holiday_checker=jpholiday.is_holiday)


def is_trading_session(
    now: datetime,
    open_hour: int,
    open_minute: int,
    close_hour: int,
    close_minute: int,
) -> bool:
    """日本の祝日を考慮した市場取引時間を判定します。"""
    return _is_trading_session(
        now,
        open_hour,
        open_minute,
        close_hour,
        close_minute,
        holiday_checker=jpholiday.is_holiday,
    )
