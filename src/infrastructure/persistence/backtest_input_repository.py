"""バックテスト入力ファイルの読み込み。"""
import csv
import json
from pathlib import Path


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
        with path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                symbol = str(row.get("symbol") or row.get("Symbol") or row.get("ticker") or row.get("Ticker"))
                close_value = row.get("close") or row.get("Close") or row.get("price") or row.get("Price")
                if not symbol or close_value is None:
                    continue
                history.setdefault(symbol, []).append(float(close_value))
        return history

    raise ValueError(f"対応していない履歴形式です: {path}")
