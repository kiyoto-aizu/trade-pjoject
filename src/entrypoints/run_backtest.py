import argparse
import csv
import json
import logging
import re
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from src.application.backtest_usecase import simulate_backtest, simulate_timeseries_backtest
from src.infrastructure.notification.line_notify import process_notification, send_line_notify

logger = logging.getLogger(__name__)


def _to_yahoo_ticker(symbol: str) -> str:
    """日本株コードをYahoo Financeのticker形式へ変換します。"""
    symbol_text = str(symbol).upper()
    if re.fullmatch(r"\d{3,4}[A-Z]?", symbol_text):
        return f"{symbol_text}.T"
    return symbol_text


def fetch_yahoo_history(symbols: list[str], days: int = 30) -> dict[str, list[float]]:
    """Yahoo Finance から最新の終値履歴を取得して、銘柄ごとの価格一覧を返す。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    history: dict[str, list[float]] = {}
    for symbol in symbols:
        symbol_text = str(symbol)
        ticker = _to_yahoo_ticker(symbol_text)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        payload = None
        try:
            response = requests.get(url, params={"interval": "1d", "range": f"{days}d"}, headers=headers, timeout=30)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            if hasattr(response, "json") and callable(response.json):
                payload = response.json()
            elif hasattr(response, "read"):
                raw = response.read()
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            else:
                raise ValueError("unsupported response type")
        except Exception as exc:
            logger.warning("%s の価格取得(requests)に失敗しました: %s", symbol_text, exc)
            try:
                request = urllib.request.Request(
                    url + f"?interval=1d&range={days}d",
                    headers=headers,
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as fallback_exc:
                logger.warning("%s の価格取得(urllib)に失敗しました: %s", symbol_text, fallback_exc)
                continue

        result = payload.get("chart", {}).get("result", [])
        if not result:
            continue

        closes: list[float] = []
        for quote in result[0].get("indicators", {}).get("quote", []):
            for close in quote.get("close", []):
                if close is not None:
                    closes.append(float(close))

        if closes:
            history[symbol_text] = closes[-days:]
    return history


def load_symbols(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"銘柄ファイルが見つかりません: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "symbols" in data:
            return [str(symbol) for symbol in data["symbols"]]
        return [str(symbol) for symbol in data.keys()]
    if isinstance(data, list):
        return [str(symbol) for symbol in data]
    raise ValueError(f"銘柄ファイルの形式が不正です: {path}")


def load_daily_filtering_symbols(directory: Path) -> dict[str, list[str]]:
    """日付別のフィルタリング結果を読み込みます。"""
    if not directory.exists():
        raise FileNotFoundError(f"フィルタリング結果ディレクトリが見つかりません: {directory}")

    daily_symbols: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        date_text = str(data.get("date") or path.stem) if isinstance(data, dict) else path.stem
        daily_symbols[date_text] = load_symbols(path)
    return daily_symbols


def fetch_yahoo_dated_history(symbols: list[str], days: int = 90) -> dict[str, dict[str, float]]:
    """Yahoo Financeから日付付きの日足終値を取得します。"""
    dated_history: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        symbol_text = str(symbol)
        ticker = _to_yahoo_ticker(symbol_text)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        payload = None
        try:
            response = requests.get(
                url,
                params={"interval": "1d", "range": f"{days}d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("%s の日付付き価格取得に失敗しました: %s", symbol_text, exc)
            continue

        result = payload.get("chart", {}).get("result", [])
        if not result:
            continue
        timestamps = result[0].get("timestamp", [])
        quotes = result[0].get("indicators", {}).get("quote", [])
        closes = quotes[0].get("close", []) if quotes else []
        history = {
            datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(): float(close)
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        }
        if history:
            dated_history[symbol_text] = history
    return dated_history


def load_history(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        raise FileNotFoundError(f"価格履歴ファイルが見つかりません: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"価格履歴ファイルの形式が不正です: {path}")
        history: dict[str, list[float]] = {}
        for symbol, values in data.items():
            history[str(symbol)] = [float(value) for value in values]
        return history

    if suffix == ".csv":
        history: dict[str, list[float]] = {}
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = str(row.get("symbol") or row.get("Symbol") or row.get("ticker") or row.get("Ticker"))
                close_value = row.get("close") or row.get("Close") or row.get("price") or row.get("Price")
                if not symbol or close_value is None:
                    continue
                history.setdefault(symbol, []).append(float(close_value))
        return history

    raise ValueError(f"対応していない履歴形式です: {path}")


def save_backtest_result(output_path: Path, result: dict) -> Path:
    """最新結果を保存し、同じ内容を実行時刻付きの履歴として保存します。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    output_path.write_text(serialized, encoding="utf-8")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive_path = output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")
    archive_path.write_text(serialized, encoding="utf-8")
    return archive_path


def main() -> None:
    with process_notification('バックテスト', notify_lifecycle=False):
        repo_root = Path(__file__).resolve().parents[2]
        default_symbols_path = repo_root / "data" / "filtering" / "2026-09-01.json"
        default_history_path = repo_root / "data" / "backtest" / "sample_history.json"

        parser = argparse.ArgumentParser(description="バックテストを実行します")
        parser.add_argument("--symbols", type=Path, default=default_symbols_path, help="銘柄一覧のJSONファイル")
        parser.add_argument("--filtering-dir", type=Path, default=None, help="日付別フィルタリング結果のディレクトリ")
        parser.add_argument("--history", type=Path, default=default_history_path, help="銘柄ごとの終値履歴JSONファイル")
        parser.add_argument("--cash", type=float, default=100000.0, help="開始現金")
        parser.add_argument("--qty", type=int, default=100, help="1回の売買数量")
        parser.add_argument("--fee", type=float, default=0.0, help="売買手数料率 (例: 0.001 = 0.1%%)")
        parser.add_argument("--live", action="store_true", help="Yahoo Finance から実データを取得してバックテストを実行")
        parser.add_argument("--days", type=int, default=30, help="Yahoo Finance から取得する日数")
        parser.add_argument("--day-trade", action="store_true", help="デイトレードとして実行し、当日の終値で保有を強制的に決済する")
        parser.add_argument("--output", type=Path, default=None, help="結果JSONの保存先")
        args = parser.parse_args()

        if args.filtering_dir:
            if not args.live:
                raise ValueError("--filtering-dir を使う場合は --live も指定してください")
            daily_symbols = load_daily_filtering_symbols(args.filtering_dir)
            if daily_symbols:
                latest_date = max(date.fromisoformat(date_text) for date_text in daily_symbols)
                cutoff = latest_date - timedelta(days=args.days)
                daily_symbols = {
                    date_text: symbols_on_day
                    for date_text, symbols_on_day in daily_symbols.items()
                    if date.fromisoformat(date_text) >= cutoff
                }
            symbols = sorted({symbol for symbols_on_day in daily_symbols.values() for symbol in symbols_on_day})
            history = fetch_yahoo_dated_history(symbols, days=args.days + 5)
            result = simulate_timeseries_backtest(
                daily_symbols,
                history,
                starting_cash=args.cash,
                qty_per_trade=args.qty,
                fee_rate=args.fee,
            )
        else:
            symbols = load_symbols(args.symbols)
            history = load_history(args.history) if args.history.exists() else {}
            if args.live:
                history = fetch_yahoo_history(symbols, days=args.days)

            result = simulate_backtest(
                symbols,
                history,
                starting_cash=args.cash,
                qty_per_trade=args.qty,
                fee_rate=args.fee,
                close_at_eod=args.day_trade,
            )

        archive_path = None
        if args.output:
            archive_path = save_backtest_result(args.output, result)

        # CLI 出力は日本語ラベルを優先して見やすくする
        display_result = {
            "総損益": result.get("総損益", result.get("total_pnl", 0.0)),
            "勝率": result.get("勝率", result.get("win_rate", 0.0)),
            "利益因子": result.get("利益因子", result.get("profit_factor", 0.0)),
            "最大ドローダウン": result.get("最大ドローダウン", result.get("max_drawdown", 0.0)),
            "総取引数": result.get("総取引数", result.get("total_trades", 0)),
            "現金残高": result.get("現金残高", result.get("cash", 0.0)),
            "最終保有数": result.get("最終保有数", result.get("final_position", 0)),
            "取引履歴": result.get("取引履歴", result.get("trade_history", [])),
            "銘柄別要約": result.get("銘柄別要約", result.get("summary_by_symbol", [])),
            "日別要約": result.get("日別要約", result.get("daily_summary", [])),
            "保有期間別要約": result.get("保有期間別要約", result.get("holding_bucket_summary", [])),
            "対象期間": f"{result['period_start']} - {result['period_end']}" if result.get("period_start") else None,
        }
        print(json.dumps(display_result, ensure_ascii=False, indent=2))
        report_lines = [
            "【バックテスト結果】",
            f"対象期間: {display_result['対象期間'] or '指定なし'}",
            f"総損益: {display_result['総損益']}",
            f"勝率: {display_result['勝率']}",
            f"総取引数: {display_result['総取引数']}",
            f"最大ドローダウン: {display_result['最大ドローダウン']}",
            f"最終保有数: {display_result['最終保有数']}",
        ]
        if args.output:
            report_lines.append(f"詳細: {args.output}")
        if archive_path:
            report_lines.append(f"履歴: {archive_path}")
        send_line_notify("\n".join(report_lines))


if __name__ == "__main__":
    main()
