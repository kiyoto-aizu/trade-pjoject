from enum import Enum


class OrderSide(str, Enum):
    SELL = "1"
    BUY = "2"


class RankingType(str, Enum):
    PRICE_GAIN = "1"
    TURNOVER = "4"
