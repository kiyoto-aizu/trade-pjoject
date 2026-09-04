from __future__ import annotations

from typing import Dict, List

from src.domain.enums import OrderSide
from src.domain.models import TradeSignal
from src.domain.rules import calculate_price_limit


def _calculate_metrics(trade_results: List[float], starting_cash: float) -> dict:
    """売買結果から勝率・利益率・ドローダウンを計算する。"""
    if not trade_results:
        return {
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
        }

    wins = sum(1 for pnl in trade_results if pnl > 0)
    gross_profit = sum(max(0.0, pnl) for pnl in trade_results)
    gross_loss = abs(sum(min(0.0, pnl) for pnl in trade_results))

    running_equity = starting_cash
    peak_equity = starting_cash
    max_drawdown = 0.0
    for pnl in trade_results:
        running_equity += pnl
        peak_equity = max(peak_equity, running_equity)
        max_drawdown = max(max_drawdown, peak_equity - running_equity)

    return {
        "win_rate": wins / len(trade_results),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "max_drawdown": max_drawdown,
    }


def simulate_backtest(
    symbols: List[str],
    history_by_symbol: Dict[str, List[float]],
    starting_cash: float = 100_000.0,
    qty_per_trade: int = 100,
    fee_rate: float = 0.0,
    close_at_eod: bool = False,
    buy_threshold_ratio: float = 1.0,
    sell_threshold_ratio: float = 1.0,
    noise_band_ratio: float = 0.0,
    stop_loss_ratio: float = 0.0,
    trend_strength_ratio: float = 0.0,
) -> dict:
    """
    シンプルなバックテストを実行します。

    役割:
    - 過去5本の終値を使って買い/売りのシグナルを作る
    - 各銘柄の保有状態と平均取得単価を管理する
    - 売却時に実現損益と保有期間を計算する
    - 手数料込みで勝率・利益率・ドローダウンを評価する
    """
    # 現金・保有数・平均取得単価を初期化
    cash = float(starting_cash)
    holdings: dict[str, int] = {symbol: 0 for symbol in symbols}
    avg_cost: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    buy_index: dict[str, int] = {symbol: -1 for symbol in symbols}
    total_trades = 0
    signals: list[dict] = []
    trade_results: List[float] = []
    trade_history: list[dict] = []
    symbols_visited: set[str] = set()

    for symbol in symbols:
        closes = history_by_symbol.get(symbol, [])
        if len(closes) < 5:
            continue

        for index, price in enumerate(closes):
            # デイトレードでは、翌日の終値を迎える前に保有を閉じる
            if close_at_eod and holdings[symbol] > 0 and index > buy_index[symbol]:
                qty = holdings[symbol]
                fee = price * qty * fee_rate
                proceeds = price * qty
                realized_pnl = (price - avg_cost[symbol]) * qty - fee
                cash += proceeds - fee
                trade_results.append(realized_pnl)
                trade_history.append({
                    "symbol": symbol,
                    "buy_price": avg_cost[symbol],
                    "sell_price": price,
                    "qty": qty,
                    "fee": fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "entry_day": buy_index[symbol],
                    "exit_day": index,
                    "holding_days": max(0, index - buy_index[symbol]),
                })
                symbols_visited.add(symbol)
                holdings[symbol] = 0
                avg_cost[symbol] = 0.0
                buy_index[symbol] = -1
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": OrderSide.SELL.value,
                    "price": price,
                    "qty": qty,
                    "fee": fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "note": "close_at_eod",
                })

            if holdings[symbol] > 0 and stop_loss_ratio > 0:
                stop_limit = avg_cost[symbol] * (1.0 - stop_loss_ratio)
                if price <= stop_limit:
                    qty = holdings[symbol]
                    fee = price * qty * fee_rate
                    proceeds = price * qty
                    realized_pnl = (price - avg_cost[symbol]) * qty - fee
                    cash += proceeds - fee
                    trade_results.append(realized_pnl)
                    trade_history.append({
                        "symbol": symbol,
                        "buy_price": avg_cost[symbol],
                        "sell_price": price,
                        "qty": qty,
                        "fee": fee,
                        "realized_pnl": round(realized_pnl, 2),
                        "entry_day": buy_index[symbol],
                        "exit_day": index,
                        "holding_days": max(0, index - buy_index[symbol]),
                    })
                    symbols_visited.add(symbol)
                    holdings[symbol] = 0
                    avg_cost[symbol] = 0.0
                    buy_index[symbol] = -1
                    total_trades += 1
                    signals.append({
                        "symbol": symbol,
                        "side": OrderSide.SELL.value,
                        "price": price,
                        "qty": qty,
                        "fee": fee,
                        "realized_pnl": round(realized_pnl, 2),
                        "note": "stop_loss",
                    })
                    continue

            window = closes[max(0, index - 5): index]
            if len(window) < 5:
                continue
            limit = calculate_price_limit(window)
            if limit is None:
                continue

            if noise_band_ratio > 0:
                avg_price = (limit.buy + limit.sell) / 2.0
                if abs(price - avg_price) / avg_price < noise_band_ratio:
                    continue

            if trend_strength_ratio > 0:
                base_mean = (limit.buy + limit.sell) / 2.0
                direction_strength = abs(price - base_mean) / base_mean
                if direction_strength < trend_strength_ratio:
                    continue

            base_buy = limit.buy
            base_sell = limit.sell
            buy_threshold = base_buy * buy_threshold_ratio
            sell_threshold = base_sell * sell_threshold_ratio
            if price <= buy_threshold:
                signal = TradeSignal(symbol=symbol, side=OrderSide.BUY, price=price, qty=qty_per_trade)
            elif price >= sell_threshold:
                signal = TradeSignal(symbol=symbol, side=OrderSide.SELL, price=price, qty=qty_per_trade)
            else:
                signal = None
            if signal is None:
                continue

            if signal.side == OrderSide.BUY and holdings[symbol] == 0 and cash >= price * qty_per_trade:
                qty = qty_per_trade
                fee = price * qty * fee_rate
                cash -= (price * qty) + fee
                holdings[symbol] = qty
                avg_cost[symbol] = price
                buy_index[symbol] = index
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": signal.side.value,
                    "price": price,
                    "qty": qty,
                    "fee": fee,
                })
            elif signal.side == OrderSide.SELL and holdings[symbol] > 0:
                qty = holdings[symbol]
                fee = price * qty * fee_rate
                proceeds = price * qty
                realized_pnl = (price - avg_cost[symbol]) * qty - fee
                cash += proceeds - fee
                trade_results.append(realized_pnl)
                holding_days = max(0, index - buy_index[symbol])
                trade_history.append({
                    "symbol": symbol,
                    "buy_price": avg_cost[symbol],
                    "sell_price": price,
                    "qty": qty,
                    "fee": fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "entry_day": buy_index[symbol],
                    "exit_day": index,
                    "holding_days": holding_days,
                })
                symbols_visited.add(symbol)
                holdings[symbol] = 0
                avg_cost[symbol] = 0.0
                buy_index[symbol] = -1
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": signal.side.value,
                    "price": price,
                    "qty": qty,
                    "fee": fee,
                    "realized_pnl": round(realized_pnl, 2),
                })

    final_position = sum(holdings.values())
    equity = cash + sum(
        holdings[symbol] * history_by_symbol.get(symbol, [])[-1] for symbol in symbols if holdings[symbol] > 0
    )
    total_pnl = round(equity - starting_cash, 2)
    metrics = _calculate_metrics(trade_results, starting_cash)

    # 銘柄ごとの成績を要約し、どの銘柄が効いているかを見る
    summary_by_symbol: list[dict] = []
    for symbol in sorted(symbols_visited | set(symbols)):
        symbol_trades = [entry for entry in trade_history if entry["symbol"] == symbol]
        if not symbol_trades:
            continue
        total_realized_pnl = round(sum(entry["realized_pnl"] for entry in symbol_trades), 2)
        avg_holding_days = round(sum(entry["holding_days"] for entry in symbol_trades) / len(symbol_trades), 2)
        summary_by_symbol.append({
            "symbol": symbol,
            "trade_count": len(symbol_trades),
            "total_realized_pnl": total_realized_pnl,
            "avg_holding_days": avg_holding_days,
            "win_count": sum(1 for entry in symbol_trades if entry["realized_pnl"] > 0),
        })

    # 日ごとの成績を要約して、期間内のどの日がよく/悪く動いたかを見える化する
    daily_summary: list[dict] = []
    daily_map: dict[int, list[dict]] = {}
    for entry in trade_history:
        day_index = entry.get("exit_day", entry.get("day_index", entry.get("holding_days", 0)))
        daily_map.setdefault(day_index, []).append(entry)
    for day_index in sorted(daily_map):
        entries = daily_map[day_index]
        daily_summary.append({
            "day_index": day_index,
            "trade_count": len(entries),
            "total_realized_pnl": round(sum(entry["realized_pnl"] for entry in entries), 2),
            "win_count": sum(1 for entry in entries if entry["realized_pnl"] > 0),
            "avg_realized_pnl": round(sum(entry["realized_pnl"] for entry in entries) / len(entries), 2),
        })

    # 保有期間別の成績で、短期と長期の動きを比較する
    holding_bucket_summary: list[dict] = []
    buckets = {
        "1-3": lambda days: 1 <= days <= 3,
        "4-7": lambda days: 4 <= days <= 7,
        "8+": lambda days: days >= 8,
    }
    for bucket_name, predicate in buckets.items():
        bucket_trades = [entry for entry in trade_history if predicate(entry["holding_days"])]
        if not bucket_trades:
            continue
        holding_bucket_summary.append({
            "bucket": bucket_name,
            "trade_count": len(bucket_trades),
            "total_realized_pnl": round(sum(entry["realized_pnl"] for entry in bucket_trades), 2),
            "avg_holding_days": round(sum(entry["holding_days"] for entry in bucket_trades) / len(bucket_trades), 2),
        })

    win_rate = round(metrics["win_rate"], 4)
    profit_factor = round(metrics["profit_factor"], 4) if metrics["profit_factor"] != float("inf") else float("inf")
    max_drawdown = round(metrics["max_drawdown"], 2)

    return {
        # 既存のキーは互換性維持のため残す
        "cash": round(cash, 2),
        "final_position": final_position,
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "max_drawdown": max_drawdown,
        "profit_factor": profit_factor,
        "signals": signals,
        "trade_history": trade_history,
        "summary_by_symbol": summary_by_symbol,
        "daily_summary": daily_summary,
        "holding_bucket_summary": holding_bucket_summary,
        # 日本語で読みやすくした主要指標
        "現金残高": round(cash, 2),
        "最終保有数": final_position,
        "総取引数": total_trades,
        "総損益": total_pnl,
        "勝率": win_rate,
        "最大ドローダウン": max_drawdown,
        "利益因子": profit_factor,
        "シグナル一覧": signals,
        "取引履歴": trade_history,
        "銘柄別要約": summary_by_symbol,
        "日別要約": daily_summary,
        "保有期間別要約": holding_bucket_summary,
    }
