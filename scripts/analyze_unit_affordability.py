"""直近フィルタリング候補の単元購入可能数を集計する調査スクリプト。"""
from __future__ import annotations

import statistics
import sys
from datetime import date, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config
from src.domain.affordability import calc_affordable_stocks
from src.domain.models import Candidate
from src.domain.rules import calculate_buy_quantity
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository


PRICE_LOOKBACK_DAYS = 14


def calculate_affordable_quantity(price: float, budget_per_position: float) -> int:
    """1銘柄予算内で買える数量を、既存ドメインルールで算出する。"""
    candidate = Candidate("analysis", price, 1)
    if not calc_affordable_stocks([candidate], budget_per_position, config.ORDER_UNIT):
        return 0
    # calculate_buy_quantity は allocate_budget と同じ
    # int(budget // (price * ORDER_UNIT)) * ORDER_UNIT を実装している。
    return calculate_buy_quantity(price, budget_per_position, config.ORDER_UNIT)


def get_latest_close(client: YahooFinanceClient, symbol: str, today: date) -> float | None:
    """休場日を遡り、Yahoo Financeから取得できる直近終値を返す。"""
    for offset in range(PRICE_LOOKBACK_DAYS):
        target_date = today - timedelta(days=offset)
        try:
            market_data = client.get_daily_market_data(symbol, target_date)
        except Exception:
            continue
        if market_data is not None and market_data.get("close", 0) > 0:
            return float(market_data["close"])
    return None


def print_price_statistics(label: str, prices: list[float]) -> None:
    if not prices:
        return
    print(f"{label}平均株価: {statistics.mean(prices):,.2f}円")
    print(f"{label}中央値株価: {statistics.median(prices):,.2f}円")


def main() -> None:
    repository = FilteringResultRepository(PROJECT_ROOT / "data" / "filtering")
    filtering_result = repository.load_latest()
    if filtering_result is None or not filtering_result.symbols:
        raise SystemExit(
            "フィルタリング結果がありません。先に run_filtering.py 等でフィルタリング結果を作成してください。"
        )

    budget_per_position = min(
        config.OPERATING_CAPITAL / config.TARGET_POSITIONS,
        config.MAX_ORDER_AMOUNT_PER_TRADE,
    )
    client = YahooFinanceClient()
    categories: dict[str, list[tuple[str, float]]] = {
        "0単元(買えない)": [],
        "1単元": [],
        "2単元以上": [],
    }
    skipped_count = 0

    for symbol in filtering_result.symbols:
        price = get_latest_close(client, symbol, date.today())
        if price is None:
            skipped_count += 1
            continue
        quantity = calculate_affordable_quantity(price, budget_per_position)
        if quantity == 0:
            category = "0単元(買えない)"
        elif quantity == config.ORDER_UNIT:
            category = "1単元"
        else:
            category = "2単元以上"
        categories[category].append((symbol, price))

    evaluated_count = sum(len(items) for items in categories.values())
    print("単元購入可能数の集計")
    print(f"フィルタリング結果: {len(filtering_result.symbols)}銘柄")
    print(f"1銘柄あたり予算: {budget_per_position:,.0f}円")
    print(f"価格取得成功: {evaluated_count}銘柄 / スキップ: {skipped_count}銘柄")

    for category, items in categories.items():
        percentage = len(items) / evaluated_count * 100 if evaluated_count else 0
        print(f"{category}: {len(items)}銘柄 ({percentage:.1f}%)")

    all_prices = [price for items in categories.values() for _, price in items]
    print_price_statistics("全評価銘柄の", all_prices)

    one_unit_items = categories["1単元"]
    print_price_statistics("1単元銘柄の", [price for _, price in one_unit_items])
    if one_unit_items:
        print("\nCAUTION時のロット削減が効かない1単元銘柄:")
        for symbol, price in one_unit_items:
            print(f"- {symbol}: {price:,.2f}円")


if __name__ == "__main__":
    main()