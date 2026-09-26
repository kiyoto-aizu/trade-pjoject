"""②フィルタ(出来高急増率)とエントリー条件(SMA×RSI)の整合性を診断する調査スクリプト。

過去のフィルタリング結果(data/filtering/*.json)に登場した各銘柄について、
選ばれた当日にエントリー条件(価格モメンタム / RSI)のどちらが成立していたか/
いなかったかを、本番と同じドメイン関数(calculate_price_limit / calculate_rsi)を
使って集計する。読み取り専用の分析であり、本番コード・閾値は一切変更しない。

実行方法:
    python scripts/analysis/analyze_filter_entry_alignment.py

【注意】
・MarketRegimeによる閾値変動(CAUTION時はRSI60以上)は考慮せず、
  NORMAL基準(RSI55以上)のみで判定しています。実際の運用より
  「エントリー条件成立」が多めに出ている可能性があります。
・日中の値動きはYahoo日足の高値(High)で代用しており、実際の
  ポーリング間隔での検知漏れ・スリッページは考慮していません。
・予算内購入可否は「upper_band価格そのもので買えるか」で判定する保守的な
  下限であり、実際の約定価格はより高くなるため、購入可能と判定していても
  実際には買えない場合があります(逆に「不可」判定はより確実です)。
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api import request_handler  # noqa: E402
from src.config import config  # noqa: E402
from src.domain.rules import calculate_buy_quantity, calculate_price_limit, calculate_rsi  # noqa: E402

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))

FILTERING_DIR = PROJECT_ROOT / "data" / "filtering"

CATEGORY_BOTH_OK_AFFORDABLE = "エントリー条件成立・予算内で購入可能"
CATEGORY_BOTH_OK_UNAFFORDABLE = "エントリー条件成立・株価が予算超過で購入不可"
CATEGORY_RSI_ONLY = "RSIは足りていたが値幅が届かなかった"
CATEGORY_PRICE_ONLY = "値上がりはしたがRSIが届かなかった"
CATEGORY_NEITHER = "どちらも届かなかった"
CATEGORY_INSUFFICIENT_DATA = "データ不足で判定不能"
CATEGORY_NO_BAR = "当日データなし(休場日など)"

ALL_CATEGORIES = [
    CATEGORY_BOTH_OK_AFFORDABLE,
    CATEGORY_BOTH_OK_UNAFFORDABLE,
    CATEGORY_RSI_ONLY,
    CATEGORY_PRICE_ONLY,
    CATEGORY_NEITHER,
    CATEGORY_INSUFFICIENT_DATA,
    CATEGORY_NO_BAR,
]

# 1銘柄あたりの発注予算。本番の予算算出ロジック(OPERATING_CAPITAL/TARGET_POSITIONS と
# MAX_ORDER_AMOUNT_PER_TRADE の小さい方)をそのまま踏襲する。
BUDGET_PER_POSITION = min(
    config.OPERATING_CAPITAL / config.TARGET_POSITIONS,
    config.MAX_ORDER_AMOUNT_PER_TRADE,
)


@dataclass(frozen=True)
class DatedBar:
    """日付付きの確定日足OHLC。"""

    trade_date: date
    high: float
    low: float
    close: float


def load_filtering_results() -> list[tuple[date, list[str]]]:
    """data/filtering配下の全フィルタリング結果を(日付, 銘柄リスト)の形で読み込む。"""
    if not FILTERING_DIR.exists():
        raise SystemExit(
            f"フィルタリング結果ディレクトリが見つかりません: {FILTERING_DIR}"
        )

    results: list[tuple[date, list[str]]] = []
    for path in sorted(FILTERING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result_date = date.fromisoformat(data["date"])
            symbols = list(data["symbols"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("フィルタリング結果の読み込みに失敗しました (%s): %s", path, exc)
            continue
        results.append((result_date, symbols))

    if not results:
        raise SystemExit("読み込めるフィルタリング結果がありませんでした。")
    return results


def fetch_dated_bars(symbol: str) -> list[DatedBar]:
    """Yahoo Financeから日付付きの確定日足OHLCを取得する(直近1年分)。

    既存の get_yahoo_daily_bars / get_yahoo_daily_closes は「呼び出した瞬間の
    "今日"より前」を基準にしており日付情報も保持しないため、過去の特定日を
    基準にした計算にはそのまま使えない。ここでは日付付きで取得し直す。
    """
    yf_symbol = f"{symbol}.T"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
    try:
        response = request_handler.send_get(
            url,
            params={"interval": "1d", "range": "1y"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
    except Exception as exc:  # pragma: no cover - ネットワーク例外は握りつぶして継続
        logger.warning("Yahooデータの取得に失敗しました (%s): %s", symbol, exc)
        return []

    if not response:
        return []

    try:
        result = response.get("chart", {}).get("result", [])
        if not result:
            return []
        chart_result = result[0]
        timestamps = chart_result.get("timestamp", [])
        quote = chart_result.get("indicators", {}).get("quote", [{}])[0]
        raw_highs = quote.get("high", [])
        raw_lows = quote.get("low", [])
        raw_closes = quote.get("close", [])
        bars = []
        for timestamp, high, low, close in zip(timestamps, raw_highs, raw_lows, raw_closes):
            if high is None or low is None or close is None:
                continue
            trade_date = datetime.fromtimestamp(timestamp, JST).date()
            bars.append(DatedBar(trade_date=trade_date, high=float(high), low=float(low), close=float(close)))
        bars.sort(key=lambda bar: bar.trade_date)
        return bars
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.warning("Yahooデータの解析に失敗しました (%s): %s", symbol, exc)
        return []


def classify(bars: list[DatedBar], target_date: date) -> str:
    """1つの(日付, 銘柄)についてエントリー条件の成否を分類する。"""
    closes_before = [bar.close for bar in bars if bar.trade_date < target_date]

    limit = calculate_price_limit(closes_before, period=5)
    rsi = calculate_rsi(closes_before, period=config.RSI_PERIOD, minimum_closes=config.RSI_MINIMUM_CLOSES)
    if limit is None or rsi is None:
        return CATEGORY_INSUFFICIENT_DATA

    today_bar: Optional[DatedBar] = next((bar for bar in bars if bar.trade_date == target_date), None)
    if today_bar is None:
        return CATEGORY_NO_BAR

    price_ok = today_bar.high >= limit.upper_band
    rsi_ok = rsi >= config.RSI_ENTRY_THRESHOLD

    if price_ok and rsi_ok:
        # 実際の約定価格は分からないが、条件を満たす最安値は upper_band 自体なので、
        # upper_band で買えないならどんな約定価格でも買えない(保守的な下限での判定)。
        quantity = calculate_buy_quantity(limit.upper_band, BUDGET_PER_POSITION, config.ORDER_UNIT)
        if quantity > 0:
            return CATEGORY_BOTH_OK_AFFORDABLE
        return CATEGORY_BOTH_OK_UNAFFORDABLE
    if rsi_ok and not price_ok:
        return CATEGORY_RSI_ONLY
    if price_ok and not rsi_ok:
        return CATEGORY_PRICE_ONLY
    return CATEGORY_NEITHER


def main() -> None:
    logging.basicConfig(level=logging.WARNING)

    filtering_results = load_filtering_results()

    # 銘柄ごとのYahoo取得は1回のみにして使い回す
    bars_cache: dict[str, list[DatedBar]] = {}

    overall_counts: Counter[str] = Counter()
    per_symbol_counts: dict[str, Counter[str]] = defaultdict(Counter)
    total_pairs = 0

    for target_date, symbols in filtering_results:
        for symbol in symbols:
            total_pairs += 1
            if symbol not in bars_cache:
                bars_cache[symbol] = fetch_dated_bars(symbol)
            bars = bars_cache[symbol]

            if not bars:
                category = CATEGORY_INSUFFICIENT_DATA
            else:
                category = classify(bars, target_date)

            overall_counts[category] += 1
            per_symbol_counts[symbol][category] += 1

    print("②フィルタとエントリー条件の整合性診断")
    print(f"対象フィルタリング結果: {len(filtering_results)}日分")
    print(f"集計対象(日付×銘柄)の総数: {total_pairs}件\n")

    print("【全体集計】")
    for category in ALL_CATEGORIES:
        count = overall_counts.get(category, 0)
        percentage = (count / total_pairs * 100) if total_pairs else 0.0
        print(f"- {category}: {count}件 ({percentage:.1f}%)")

    print("\n【銘柄別内訳】")
    header = "銘柄".ljust(10) + "".join(cat[:6].ljust(10) for cat in ALL_CATEGORIES)
    print(header)
    for symbol, counts in sorted(per_symbol_counts.items()):
        row = symbol.ljust(10) + "".join(str(counts.get(cat, 0)).ljust(10) for cat in ALL_CATEGORIES)
        print(row)

    print(
        "\n【注意】\n"
        "・MarketRegimeによる閾値変動(CAUTION時はRSI60以上)は考慮せず、\n"
        "  NORMAL基準(RSI55以上)のみで判定しています。実際の運用より\n"
        "  「エントリー条件成立」が多めに出ている可能性があります。\n"
        "・日中の値動きはYahoo日足の高値(High)で代用しており、実際の\n"
        "  ポーリング間隔での検知漏れ・スリッページは考慮していません。\n"
        "・予算内購入可否は「upper_band価格そのもので買えるか」で判定する保守的な\n"
        "  下限であり、実際の約定価格はより高くなるため、購入可能と判定していても\n"
        "  実際には買えない場合があります(逆に「不可」判定はより確実です)。"
    )


if __name__ == "__main__":
    main()
