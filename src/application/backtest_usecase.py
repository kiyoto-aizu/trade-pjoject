from __future__ import annotations

from datetime import date
from typing import Dict, List

from src.config import config
from src.domain.enums import OrderSide
from src.domain.models import TradeSignal
from src.domain.rules import calculate_price_limit, calculate_rsi
from src.infrastructure.persistence.minute_bar_repository import MinuteBarRepository


def _calculate_execution_price(
    side: OrderSide,
    signal_price: float,
    prices: List[float],
    signal_index: int,
    execution_delay_bars: int,
    order_type: str,
    market_slippage_bps: float,
) -> tuple[float, float]:
    """シグナル後の観測価格から、注文種別に応じた想定約定価格を返す。"""
    delayed_index = min(signal_index + execution_delay_bars, len(prices) - 1)
    delayed_price = prices[delayed_index]
    if order_type == "limit":
        return signal_price, delayed_price

    slippage_rate = market_slippage_bps / 10_000.0
    execution_price = delayed_price * (1.0 + slippage_rate if side == OrderSide.BUY else 1.0 - slippage_rate)
    return execution_price, delayed_price


def _validate_execution_assumptions(
    fee_rate: float,
    market_slippage_bps: float,
    execution_delay_bars: int,
    order_type: str,
) -> None:
    if fee_rate < 0 or market_slippage_bps < 0 or execution_delay_bars < 0:
        raise ValueError("手数料率、スリッページ、執行遅延は0以上を指定してください")
    if order_type not in {"market", "limit"}:
        raise ValueError("order_type は 'market' または 'limit' を指定してください")


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
    fee_rate: float | None = None,
    market_slippage_bps: float | None = None,
    execution_delay_bars: int | None = None,
    order_type: str | None = None,
    close_at_eod: bool = True,
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
    fee_rate = config.BACKTEST_FEE_RATE if fee_rate is None else fee_rate
    market_slippage_bps = config.BACKTEST_MARKET_SLIPPAGE_BPS if market_slippage_bps is None else market_slippage_bps
    execution_delay_bars = config.BACKTEST_EXECUTION_DELAY_BARS if execution_delay_bars is None else execution_delay_bars
    order_type = config.BACKTEST_ORDER_TYPE if order_type is None else order_type
    _validate_execution_assumptions(fee_rate, market_slippage_bps, execution_delay_bars, order_type)
    # 現金・保有数・平均取得単価を初期化
    cash = float(starting_cash)
    holdings: dict[str, int] = {symbol: 0 for symbol in symbols}
    avg_cost: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    entry_prices: dict[str, float] = {symbol: 0.0 for symbol in symbols}
    entry_fees: dict[str, float] = {symbol: 0.0 for symbol in symbols}
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
                execution_price, delayed_price = _calculate_execution_price(
                    OrderSide.SELL, price, closes, index, execution_delay_bars, order_type, market_slippage_bps,
                )
                fee = execution_price * qty * fee_rate
                proceeds = execution_price * qty
                realized_pnl = (execution_price - avg_cost[symbol]) * qty - fee
                cash += proceeds - fee
                trade_results.append(realized_pnl)
                trade_history.append({
                    "symbol": symbol,
                    "buy_price": entry_prices[symbol],
                    "sell_price": execution_price,
                    "signal_price": price,
                    "delayed_price": delayed_price,
                    "entry_fee": entry_fees[symbol],
                    "exit_fee": fee,
                    "qty": qty,
                    "fee": entry_fees[symbol] + fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "entry_day": buy_index[symbol],
                    "exit_day": index,
                    "holding_days": max(0, index - buy_index[symbol]),
                })
                symbols_visited.add(symbol)
                holdings[symbol] = 0
                avg_cost[symbol] = 0.0
                entry_prices[symbol] = 0.0
                entry_fees[symbol] = 0.0
                buy_index[symbol] = -1
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": OrderSide.SELL.value,
                    "price": execution_price,
                    "signal_price": price,
                    "delayed_price": delayed_price,
                    "qty": qty,
                    "fee": fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "note": "close_at_eod",
                })

            if holdings[symbol] > 0 and stop_loss_ratio > 0:
                stop_limit = avg_cost[symbol] * (1.0 - stop_loss_ratio)
                if price <= stop_limit:
                    qty = holdings[symbol]
                    execution_price, delayed_price = _calculate_execution_price(
                        OrderSide.SELL, price, closes, index, execution_delay_bars, order_type, market_slippage_bps,
                    )
                    fee = execution_price * qty * fee_rate
                    proceeds = execution_price * qty
                    realized_pnl = (execution_price - avg_cost[symbol]) * qty - fee
                    cash += proceeds - fee
                    trade_results.append(realized_pnl)
                    trade_history.append({
                        "symbol": symbol,
                        "buy_price": entry_prices[symbol],
                        "sell_price": execution_price,
                        "signal_price": price,
                        "delayed_price": delayed_price,
                        "entry_fee": entry_fees[symbol],
                        "exit_fee": fee,
                        "qty": qty,
                        "fee": entry_fees[symbol] + fee,
                        "realized_pnl": round(realized_pnl, 2),
                        "entry_day": buy_index[symbol],
                        "exit_day": index,
                        "holding_days": max(0, index - buy_index[symbol]),
                    })
                    symbols_visited.add(symbol)
                    holdings[symbol] = 0
                    avg_cost[symbol] = 0.0
                    entry_prices[symbol] = 0.0
                    entry_fees[symbol] = 0.0
                    buy_index[symbol] = -1
                    total_trades += 1
                    signals.append({
                        "symbol": symbol,
                        "side": OrderSide.SELL.value,
                        "price": execution_price,
                        "signal_price": price,
                        "delayed_price": delayed_price,
                        "qty": qty,
                        "fee": fee,
                        "realized_pnl": round(realized_pnl, 2),
                        "note": "stop_loss",
                    })
                    continue

            window = closes[max(0, index - config.RSI_MINIMUM_CLOSES): index]
            if len(window) < config.RSI_MINIMUM_CLOSES:
                continue
            limit = calculate_price_limit(window)
            rsi = calculate_rsi(window, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
            if limit is None or rsi is None:
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
            adjusted_limit = type(limit)(buy=buy_threshold, sell=sell_threshold)
            signal = TradeSignal.evaluate(
                symbol,
                price,
                adjusted_limit,
                rsi,
                config.RSI_ENTRY_THRESHOLD,
                config.RSI_EXIT_THRESHOLD,
            )
            if signal is None:
                signal = None
            else:
                signal.qty = qty_per_trade
            if signal is None:
                continue

            execution_price, delayed_price = _calculate_execution_price(
                signal.side, price, closes, index, execution_delay_bars, order_type, market_slippage_bps,
            )
            if signal.side == OrderSide.BUY and holdings[symbol] == 0 and cash >= execution_price * qty_per_trade * (1.0 + fee_rate):
                qty = qty_per_trade
                fee = execution_price * qty * fee_rate
                cash -= (execution_price * qty) + fee
                holdings[symbol] = qty
                avg_cost[symbol] = execution_price + fee / qty
                entry_prices[symbol] = execution_price
                entry_fees[symbol] = fee
                buy_index[symbol] = index
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": signal.side.value,
                    "price": execution_price,
                    "signal_price": price,
                    "delayed_price": delayed_price,
                    "qty": qty,
                    "fee": fee,
                })
            elif signal.side == OrderSide.SELL and holdings[symbol] > 0:
                qty = holdings[symbol]
                fee = execution_price * qty * fee_rate
                proceeds = execution_price * qty
                realized_pnl = (execution_price - avg_cost[symbol]) * qty - fee
                cash += proceeds - fee
                trade_results.append(realized_pnl)
                holding_days = max(0, index - buy_index[symbol])
                trade_history.append({
                    "symbol": symbol,
                    "buy_price": entry_prices[symbol],
                    "sell_price": execution_price,
                    "signal_price": price,
                    "delayed_price": delayed_price,
                    "entry_fee": entry_fees[symbol],
                    "exit_fee": fee,
                    "qty": qty,
                    "fee": entry_fees[symbol] + fee,
                    "realized_pnl": round(realized_pnl, 2),
                    "entry_day": buy_index[symbol],
                    "exit_day": index,
                    "holding_days": holding_days,
                })
                symbols_visited.add(symbol)
                holdings[symbol] = 0
                avg_cost[symbol] = 0.0
                entry_prices[symbol] = 0.0
                entry_fees[symbol] = 0.0
                buy_index[symbol] = -1
                total_trades += 1
                signals.append({
                    "symbol": symbol,
                    "side": signal.side.value,
                    "price": execution_price,
                    "signal_price": price,
                    "delayed_price": delayed_price,
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
        "execution_assumptions": {
            "fee_rate": fee_rate,
            "order_type": order_type,
            "market_slippage_bps": market_slippage_bps,
            "execution_delay_bars": execution_delay_bars,
        },
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


def simulate_timeseries_backtest(
    daily_symbols: Dict[str, List[str]],
    dated_history_by_symbol: Dict[str, Dict[str, float]],
    starting_cash: float = 100_000.0,
    qty_per_trade: int = 100,
    fee_rate: float | None = None,
    market_slippage_bps: float | None = None,
    execution_delay_bars: int | None = None,
    order_type: str | None = None,
    buy_threshold_ratio: float = 1.0,
    sell_threshold_ratio: float = 1.0,
    noise_band_ratio: float = 0.0,
    stop_loss_ratio: float = 0.0,
    trend_strength_ratio: float = 0.0,
    minute_bar_repository: MinuteBarRepository | None = None,
    indicator_source: str = "daily",
    close_at_eod: bool = True,
) -> dict:
    """日付ごとのフィルタリング結果を、日足または分足で再生します。"""
    if indicator_source not in {"daily", "minute"}:
        raise ValueError("indicator_source は 'daily' または 'minute' を指定してください")

    fee_rate = config.BACKTEST_FEE_RATE if fee_rate is None else fee_rate
    market_slippage_bps = config.BACKTEST_MARKET_SLIPPAGE_BPS if market_slippage_bps is None else market_slippage_bps
    execution_delay_bars = config.BACKTEST_EXECUTION_DELAY_BARS if execution_delay_bars is None else execution_delay_bars
    order_type = config.BACKTEST_ORDER_TYPE if order_type is None else order_type
    _validate_execution_assumptions(fee_rate, market_slippage_bps, execution_delay_bars, order_type)
    cash = float(starting_cash)
    holdings: dict[str, int] = {}
    avg_cost: dict[str, float] = {}
    entry_prices: dict[str, float] = {}
    entry_fees: dict[str, float] = {}
    buy_dates: dict[str, str] = {}
    buy_times: dict[str, str] = {}
    trade_results: list[float] = []
    trade_history: list[dict] = []
    signals: list[dict] = []
    all_symbols = set(dated_history_by_symbol) | {
        symbol for symbols in daily_symbols.values() for symbol in symbols
    }
    minute_prices_by_symbol: dict[str, list[float]] = {}
    last_prices: dict[str, float] = {}
    minute_signal_evaluations = 0
    daily_close_signal_evaluations = 0

    def close_position(
        symbol: str,
        date_text: str,
        price: float,
        time_text: str | None = None,
        note: str | None = None,
    ) -> None:
        nonlocal cash
        qty = holdings.get(symbol, 0)
        if qty <= 0:
            return
        execution_price, delayed_price = _calculate_execution_price(
            OrderSide.SELL,
            price,
            prices_by_symbol[symbol],
            current_bar_indices[symbol],
            execution_delay_bars,
            order_type,
            market_slippage_bps,
        )
        exit_fee = execution_price * qty * fee_rate
        realized_pnl = (execution_price - avg_cost[symbol]) * qty - exit_fee
        cash += execution_price * qty - exit_fee
        trade_results.append(realized_pnl)
        entry_date = buy_dates[symbol]
        holding_days = max(0, (date.fromisoformat(date_text) - date.fromisoformat(entry_date)).days)
        trade_history.append({
            "symbol": symbol,
            "buy_price": entry_prices[symbol],
            "sell_price": execution_price,
            "signal_price": price,
            "delayed_price": delayed_price,
            "qty": qty,
            "entry_fee": entry_fees[symbol],
            "exit_fee": exit_fee,
            "fee": entry_fees[symbol] + exit_fee,
            "realized_pnl": round(realized_pnl, 2),
            "entry_date": entry_date,
            "exit_date": date_text,
            "holding_days": holding_days,
        })
        if symbol in buy_times:
            trade_history[-1]["entry_time"] = buy_times[symbol]
        if time_text:
            trade_history[-1]["exit_time"] = time_text
        signal = {
            "symbol": symbol,
            "side": OrderSide.SELL.value,
            "price": execution_price,
            "signal_price": price,
            "delayed_price": delayed_price,
            "qty": qty,
            "fee": exit_fee,
            "realized_pnl": round(realized_pnl, 2),
        }
        if time_text:
            signal["time"] = time_text
        if note:
            signal["note"] = note
        signals.append(signal)
        holdings[symbol] = 0
        avg_cost[symbol] = 0.0
        entry_prices.pop(symbol, None)
        entry_fees.pop(symbol, None)
        buy_dates.pop(symbol, None)
        buy_times.pop(symbol, None)

    def evaluate_signal(symbol: str, date_text: str, time_text: str | None, price: float) -> TradeSignal | None:
        nonlocal minute_signal_evaluations, daily_close_signal_evaluations
        if time_text is None:
            daily_close_signal_evaluations += 1
        else:
            minute_signal_evaluations += 1
        if indicator_source == "minute" and time_text is not None:
            previous_prices = minute_prices_by_symbol.get(symbol, [])[-config.RSI_MINIMUM_CLOSES:]
        else:
            previous_prices = [
                price_value
                for history_date, price_value in sorted(dated_history_by_symbol.get(symbol, {}).items())
                if history_date < date_text
            ][-config.RSI_MINIMUM_CLOSES:]
        if len(previous_prices) < config.RSI_MINIMUM_CLOSES:
            return None
        limit = calculate_price_limit(previous_prices)
        rsi = calculate_rsi(previous_prices, config.RSI_PERIOD, config.RSI_MINIMUM_CLOSES)
        if limit is None or rsi is None:
            return None

        base_mean = (limit.buy + limit.sell) / 2.0
        within_noise_band = noise_band_ratio > 0 and abs(price - base_mean) / base_mean < noise_band_ratio
        weak_trend = trend_strength_ratio > 0 and abs(price - base_mean) / base_mean < trend_strength_ratio
        if within_noise_band or weak_trend:
            return None
        adjusted_limit = type(limit)(
            buy=limit.buy * buy_threshold_ratio,
            sell=limit.sell * sell_threshold_ratio,
        )
        return TradeSignal.evaluate(
            symbol,
            price,
            adjusted_limit,
            rsi,
            config.RSI_ENTRY_THRESHOLD,
            config.RSI_EXIT_THRESHOLD,
        )

    for date_text in sorted(daily_symbols):
        active_symbols = set(daily_symbols[date_text])
        ticks_by_time: dict[str, list[tuple[str, float, str | None]]] = {}
        prices_by_symbol: dict[str, list[float]] = {}
        current_bar_indices: dict[str, int] = {}
        for symbol in sorted(all_symbols):
            bars = (
                minute_bar_repository.load_bars(date.fromisoformat(date_text), symbol)
                if minute_bar_repository is not None
                else []
            )
            if bars:
                prices_by_symbol[symbol] = [bar.price for bar in bars]
                for bar in bars:
                    ticks_by_time.setdefault(bar.time, []).append((symbol, bar.price, bar.time))
            else:
                daily_close = dated_history_by_symbol.get(symbol, {}).get(date_text)
                if daily_close is not None:
                    prices_by_symbol[symbol] = [daily_close]
                    ticks_by_time.setdefault(f"{date_text}T15:30:00", []).append((symbol, daily_close, None))

        closing_prices: dict[str, tuple[float, str | None]] = {}
        for _, ticks in sorted(ticks_by_time.items()):
            signals_at_time: list[tuple[str, float, str | None, TradeSignal]] = []
            stopped_out_symbols: set[str] = set()
            for symbol, price, time_text in sorted(ticks):
                current_bar_indices[symbol] = current_bar_indices.get(symbol, -1) + 1
                last_prices[symbol] = price
                if holdings.get(symbol, 0) > 0 and stop_loss_ratio > 0:
                    if price <= avg_cost[symbol] * (1.0 - stop_loss_ratio):
                        close_position(symbol, date_text, price, time_text, "stop_loss")
                        stopped_out_symbols.add(symbol)
                if symbol in active_symbols and symbol not in stopped_out_symbols:
                    signal = evaluate_signal(symbol, date_text, time_text, price)
                    if signal is not None:
                        signals_at_time.append((symbol, price, time_text, signal))

            for symbol, price, time_text, signal in signals_at_time:
                if signal.side == OrderSide.SELL and holdings.get(symbol, 0) > 0:
                    close_position(symbol, date_text, price, time_text)
            for symbol, price, time_text, signal in signals_at_time:
                if signal.side == OrderSide.BUY and holdings.get(symbol, 0) == 0 and cash >= price * qty_per_trade:
                    execution_price, delayed_price = _calculate_execution_price(
                        OrderSide.BUY,
                        price,
                        prices_by_symbol[symbol],
                        current_bar_indices[symbol],
                        execution_delay_bars,
                        order_type,
                        market_slippage_bps,
                    )
                    fee = execution_price * qty_per_trade * fee_rate
                    if cash < execution_price * qty_per_trade + fee:
                        continue
                    cash -= execution_price * qty_per_trade + fee
                    holdings[symbol] = qty_per_trade
                    avg_cost[symbol] = execution_price + fee / qty_per_trade
                    entry_prices[symbol] = execution_price
                    entry_fees[symbol] = fee
                    buy_dates[symbol] = date_text
                    if time_text:
                        buy_times[symbol] = time_text
                    signals.append({
                        "symbol": symbol,
                        "side": OrderSide.BUY.value,
                        "price": execution_price,
                        "signal_price": price,
                        "delayed_price": delayed_price,
                        "qty": qty_per_trade,
                        "fee": fee,
                        "date": date_text,
                        **({"time": time_text} if time_text else {}),
                    })

            for symbol, price, time_text in ticks:
                if time_text is not None:
                    minute_prices_by_symbol.setdefault(symbol, []).append(price)
                closing_prices[symbol] = (price, time_text)

        if close_at_eod:
            for symbol, quantity in list(holdings.items()):
                if quantity > 0 and symbol in closing_prices:
                    price, time_text = closing_prices[symbol]
                    close_position(symbol, date_text, price, time_text, "close_at_eod")

    last_date = max(daily_symbols) if daily_symbols else ""
    equity = cash
    for symbol, qty in holdings.items():
        mark_price = last_prices.get(symbol)
        if qty > 0 and mark_price is not None:
            equity += qty * mark_price

    metrics = _calculate_metrics(trade_results, starting_cash)
    total_pnl = round(equity - starting_cash, 2)
    return {
        "cash": round(cash, 2),
        "final_position": sum(holdings.values()),
        "total_trades": len(signals),
        "total_pnl": total_pnl,
        "win_rate": round(metrics["win_rate"], 4),
        "max_drawdown": round(metrics["max_drawdown"], 2),
        "profit_factor": metrics["profit_factor"],
        "signals": signals,
        "trade_history": trade_history,
        "period_start": min(daily_symbols) if daily_symbols else None,
        "period_end": last_date or None,
        "days": len(daily_symbols),
        "indicator_source": indicator_source,
        "minute_bar_mode": minute_bar_repository is not None,
        "minute_signal_evaluations": minute_signal_evaluations,
        "daily_close_signal_evaluations": daily_close_signal_evaluations,
        "execution_assumptions": {
            "fee_rate": fee_rate,
            "order_type": order_type,
            "market_slippage_bps": market_slippage_bps,
            "execution_delay_bars": execution_delay_bars,
        },
    }
