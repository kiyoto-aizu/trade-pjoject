"""CLIから受け取った条件でバックテストを実行するユースケース。"""
import logging
from datetime import date, timedelta
from pathlib import Path

from src.application.backtest_report_usecase import comparison_summary, market_regime_comparison_summary
from src.application.backtest_usecase import simulate_backtest, simulate_timeseries_backtest
from src.config import config
from src.domain.market_regime import calculate_market_regime_series
from src.infrastructure.market_data.yahoo_backtest_history_client import (
    fetch_yahoo_dated_history,
    fetch_yahoo_dated_ohlc,
    fetch_yahoo_history,
)
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient
from src.infrastructure.persistence.backtest_input_repository import (
    load_daily_filtering_symbols,
    load_history,
    load_symbols,
)
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

logger = logging.getLogger(__name__)


def _timeseries_kwargs(args, sizing_kwargs, minute_bar_repository, ohlc_history, market_regime_by_date):
    return {
        "starting_cash": args.cash,
        "qty_per_trade": args.qty,
        **sizing_kwargs,
        "fee_rate": args.fee,
        "market_slippage_bps": args.market_slippage_bps,
        "execution_delay_bars": args.execution_delay_bars,
        "order_type": args.order_type,
        "minute_bar_repository": minute_bar_repository,
        "indicator_source": args.indicator_source,
        "close_at_eod": not args.allow_overnight,
        "ohlc_history_by_symbol_date": ohlc_history,
        "market_regime_by_date": market_regime_by_date,
    }


def _market_regime_by_date(args):
    index_client = YahooIndexClient()
    return {
        target_date.isoformat(): regime
        for target_date, regime in calculate_market_regime_series(
            index_client.get_daily_ohlc("^N225", range_=f"{args.days + 30}d"),
            index_client.get_daily_ohlc("^VIX", range_=f"{args.days + 30}d"),
            config.MARKET_REGIME_REALIZED_VOL_WINDOW,
            config.MARKET_REGIME_THRESHOLDS,
        ).items()
    }


def _run_timeseries_comparisons(
    args, daily_symbols, dated_history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
):
    atr_comparison = None
    market_regime_comparison = None
    common = _timeseries_kwargs(
        args, sizing_kwargs, minute_bar_repository, ohlc_history, None,
    )
    if args.compare_market_regime:
        baseline = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common, market_regime_enabled=False,
        )
        market_regime_comparison = market_regime_comparison_summary(baseline, result)
    if args.compare_atr:
        baseline = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common, enable_volatility_adjustment=False,
        )
        lot_only = simulate_timeseries_backtest(
            daily_symbols, dated_history, **common,
            enable_volatility_sizing=True, enable_atr_stop_loss=False,
        )
        atr_comparison = {
            "without_atr": comparison_summary(baseline),
            "lot_adjustment_only": comparison_summary(lot_only),
            "lot_adjustment_and_stop": comparison_summary(result),
        }
    return atr_comparison, market_regime_comparison


def _run_filtering_backtest(args, sizing_kwargs, minute_bar_repository, repo_root):
    daily_symbols = load_daily_filtering_symbols(args.filtering_dir)
    if args.start_date is not None and args.end_date is not None:
        logger.warning("--start-date/--end-date を優先し、--days は無視します。")
        daily_symbols = {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if args.start_date <= date.fromisoformat(date_text) <= args.end_date
        }
    elif daily_symbols:
        latest_date = max(date.fromisoformat(date_text) for date_text in daily_symbols)
        cutoff = latest_date - timedelta(days=args.days)
        daily_symbols = {
            date_text: symbols_on_day
            for date_text, symbols_on_day in daily_symbols.items()
            if date.fromisoformat(date_text) >= cutoff
        }
    symbols = sorted({symbol for symbols_on_day in daily_symbols.values() for symbol in symbols_on_day})
    history = fetch_yahoo_dated_history(symbols, days=args.days + 5)
    ohlc_history = fetch_yahoo_dated_ohlc(symbols, days=args.days + 5) if args.compare_atr else None
    market_regime_by_date = _market_regime_by_date(args) if args.compare_market_regime else None
    result = simulate_timeseries_backtest(
        daily_symbols,
        history,
        **_timeseries_kwargs(
            args,
            sizing_kwargs,
            minute_bar_repository,
            ohlc_history,
            market_regime_by_date,
        ),
        filter_decision_repository=FilterDecisionRepository(repo_root / "data" / "filter_decision_events.sqlite3"),
    )
    atr_comparison, market_regime_comparison = _run_timeseries_comparisons(
        args, daily_symbols, history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
    )
    return result, atr_comparison, market_regime_comparison


def _run_minute_backtest(args, sizing_kwargs, minute_bar_repository, symbols):
    dated_history = fetch_yahoo_dated_history(symbols, days=args.days + 5)
    ohlc_history = fetch_yahoo_dated_ohlc(symbols, days=args.days + 5) if args.compare_atr else None
    daily_symbols = {
        date_text: symbols
        for date_text in sorted({
            date_text for symbol_history in dated_history.values() for date_text in symbol_history
        })
    }
    market_regime_by_date = _market_regime_by_date(args) if args.compare_market_regime else None
    result = simulate_timeseries_backtest(
        daily_symbols,
        dated_history,
        **_timeseries_kwargs(
            args,
            sizing_kwargs,
            minute_bar_repository,
            ohlc_history,
            market_regime_by_date,
        ),
    )
    atr_comparison, market_regime_comparison = _run_timeseries_comparisons(
        args, daily_symbols, dated_history, sizing_kwargs, minute_bar_repository, ohlc_history, result,
    )
    return result, atr_comparison, market_regime_comparison


def _run_fixed_backtest(args, symbols, history):
    ohlc_history = fetch_yahoo_dated_ohlc(symbols, days=args.days) if args.compare_atr else None
    result = simulate_backtest(
        symbols,
        history,
        starting_cash=args.cash,
        qty_per_trade=args.qty,
        fee_rate=args.fee,
        market_slippage_bps=args.market_slippage_bps,
        execution_delay_bars=args.execution_delay_bars,
        order_type=args.order_type,
        close_at_eod=not args.allow_overnight,
        ohlc_history_by_symbol={
            symbol: [bar for _, bar in sorted((ohlc_history or {}).get(symbol, {}).items())]
            for symbol in symbols
        } if ohlc_history else None,
    )
    atr_comparison = None
    if args.compare_atr:
        common = {
            "starting_cash": args.cash,
            "qty_per_trade": args.qty,
            "fee_rate": args.fee,
            "market_slippage_bps": args.market_slippage_bps,
            "execution_delay_bars": args.execution_delay_bars,
            "order_type": args.order_type,
            "close_at_eod": not args.allow_overnight,
            "ohlc_history_by_symbol": {
                symbol: [bar for _, bar in sorted((ohlc_history or {}).get(symbol, {}).items())]
                for symbol in symbols
            } if ohlc_history else None,
        }
        baseline = simulate_backtest(symbols, history, **common, enable_volatility_adjustment=False)
        lot_only = simulate_backtest(
            symbols, history, **common,
            enable_volatility_sizing=True, enable_atr_stop_loss=False,
        )
        atr_comparison = {
            "without_atr": comparison_summary(baseline),
            "lot_adjustment_only": comparison_summary(lot_only),
            "lot_adjustment_and_stop": comparison_summary(result),
        }
    return result, atr_comparison, None


def execute_backtest(args, sizing_kwargs, minute_bar_repository, repo_root):
    if args.filtering_dir:
        if not args.live:
            raise ValueError("--filtering-dir を使う場合は --live も指定してください")
        return _run_filtering_backtest(args, sizing_kwargs, minute_bar_repository, repo_root)

    symbols = load_symbols(args.symbols)
    history = load_history(args.history) if args.history.exists() else {}
    if args.live:
        history = fetch_yahoo_history(symbols, days=args.days)
    if minute_bar_repository is not None:
        if not args.live:
            raise ValueError("--minute-bars-dir を使う固定銘柄モードでは --live を指定してください")
        return _run_minute_backtest(args, sizing_kwargs, minute_bar_repository, symbols)
    return _run_fixed_backtest(args, symbols, history)
