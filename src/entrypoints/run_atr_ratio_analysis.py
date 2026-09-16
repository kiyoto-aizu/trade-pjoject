"""フィルタリング対象銘柄のATR倍率分布を分析します。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from src.domain.atr_ratio_analysis import (
    calculate_at_or_below_percentile,
    calculate_atr_ratio_series,
    calculate_reference_candidates,
    summarize_atr_ratio_distribution,
    summarize_symbol_atr_ratios,
)
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient


def load_filtering_symbols(directory: Path) -> tuple[list[str], int]:
    """全フィルタリング履歴から重複のない銘柄一覧を返します。"""
    if not directory.exists():
        raise FileNotFoundError(f"フィルタリング結果ディレクトリが見つかりません: {directory}")

    paths = sorted(directory.glob("*.json"))
    symbols = set()
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise ValueError(f"フィルタリング結果の形式が不正です: {path}")
        symbols.update(str(symbol) for symbol in data["symbols"])
    return sorted(symbols), len(paths)


def build_report(
    symbols: list[str],
    filtering_history_file_count: int,
    client: YahooFinanceClient,
    atr_period: int,
    data_range: str,
) -> dict:
    """銘柄ごとのATR倍率を集計したJSON互換レポートを構築します。"""
    points_by_symbol = {}
    excluded_symbols = []
    for symbol in symbols:
        bars = client.get_daily_ohlc_history(symbol, range_=data_range)
        if len(bars) < atr_period:
            excluded_symbols.append({"symbol": symbol, "reason": "insufficient_daily_ohlc"})
            continue
        points = calculate_atr_ratio_series(bars, period=atr_period)
        if not points:
            excluded_symbols.append({"symbol": symbol, "reason": "no_atr_ratio_points"})
            continue
        points_by_symbol[symbol] = points

    values = [point.ratio for points in points_by_symbol.values() for point in points]
    threshold_percentiles = {
        str(threshold): calculate_at_or_below_percentile(values, threshold)
        for threshold in (1.5, 2.0)
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "atr_period": atr_period,
        "data_range": data_range,
        "universe": {
            "filtering_history_file_count": filtering_history_file_count,
            "unique_symbol_count": len(symbols),
            "symbols": symbols,
            "analyzed_symbol_count": len(points_by_symbol),
            "excluded_symbols": excluded_symbols,
        },
        "overall_summary": summarize_atr_ratio_distribution(values),
        "per_symbol_summary": summarize_symbol_atr_ratios(points_by_symbol),
        "current_threshold_percentiles": {
            "caution_ratio": 1.5,
            "danger_ratio": 2.0,
            "at_or_below_percentile": threshold_percentiles,
            "upper_tail_percent": {
                threshold: None if percentile is None else 100.0 - percentile
                for threshold, percentile in threshold_percentiles.items()
            },
        },
        "reference_candidates": calculate_reference_candidates(values),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="フィルタリング対象銘柄のATR倍率分布を分析します")
    parser.add_argument("--atr-period", type=int, default=14, help="ATR期間（日数）")
    parser.add_argument("--range", dest="data_range", default="max", help="Yahoo Financeの取得範囲（例: 10y, max）")
    parser.add_argument("--filtering-directory", type=Path, default=None, help="フィルタリング結果ディレクトリ")
    parser.add_argument("--output", type=Path, default=None, help="JSON出力先")
    args = parser.parse_args()
    if args.atr_period <= 0:
        raise SystemExit("--atr-period は正数で指定してください")

    root = Path(__file__).resolve().parents[2]
    filtering_directory = args.filtering_directory or root / "data" / "filtering"
    symbols, history_file_count = load_filtering_symbols(filtering_directory)
    if not symbols:
        raise SystemExit("フィルタリング履歴に対象銘柄がありません")

    report = build_report(
        symbols,
        filtering_history_file_count=history_file_count,
        client=YahooFinanceClient(),
        atr_period=args.atr_period,
        data_range=args.data_range,
    )
    output = args.output or root / "data" / "analysis" / "atr_ratio_analysis_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()