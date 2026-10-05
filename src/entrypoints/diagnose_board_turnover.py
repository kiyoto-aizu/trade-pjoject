"""
================================================================================
板API(/board/{symbol}@1)の売買高・売買代金欠損の切り分け用診断(読み取り専用)
同一銘柄を複数回取得し、TradingVolume/TradingValueの変化と取得順・登録数・経過秒を表示します。

- 発注・状態ファイル・DB・フィルタ結果は書きません(結果JSONのみ保存)
- 登録解除は行いません(登録数が上限に近づく場合は警告のみ)
- トークンは新規発行しません(共有済みトークン、または --token を使用)
- 他の市場処理が稼働中は中止します(--force で上書き)

使い方:
    python -m src.entrypoints.diagnose_board_turnover --symbols 4564,4597 --repeat 2
================================================================================
"""
import argparse
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_session
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.kabu.board_repository import BoardRepository
from src.infrastructure.kabu.token_provider import get_token_provider
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository

JST = timezone(timedelta(hours=9))
REGISTRATION_LIMIT = 50
OUTPUT_DIRECTORY = Path(config.SCREENING_RESULT_DIRECTORY).parent / "analysis"


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="カンマ区切り。既定は直近のスクリーニング結果の全銘柄")
    parser.add_argument("--repeat", type=int, default=2, help="同一銘柄の取得回数(既定2)")
    parser.add_argument("--interval-seconds", type=float, default=0.0, help="取得間隔(秒)")
    parser.add_argument("--output", type=Path, help="結果JSONの保存先")
    parser.add_argument("--token", help="共有トークンが無い場合に手動で渡すトークン")
    parser.add_argument("--force", action="store_true", help="他の市場処理が稼働中でも実行する")
    return parser.parse_args(argv)


def _load_symbols(symbols_arg: str | None) -> list[str]:
    if symbols_arg:
        return [s.strip() for s in symbols_arg.split(",") if s.strip()]
    latest = ScreeningResultRepository(config.SCREENING_RESULT_DIRECTORY).load_latest()
    return list(latest.symbols) if latest else []


def run_diagnosis(client, symbols, repeat, interval_seconds, sleep=time.sleep, clock=time.monotonic) -> list[dict]:
    """各銘柄を repeat 回取得し、取得ごとの記録を返す。"""
    started = clock()
    rows: list[dict] = []
    registered: set[str] = set()
    seq = 0
    for symbol in symbols:
        for attempt in range(1, repeat + 1):
            if seq and interval_seconds > 0:
                sleep(interval_seconds)
            seq += 1
            registered.add(symbol)
            elapsed = round(clock() - started, 3)
            try:
                board = client.get_current_board_with_freshness(symbol)
                error = None
            except Exception as exc:
                board, error = None, type(exc).__name__
            rows.append({
                "seq": seq,
                "symbol": symbol,
                "attempt": attempt,
                "registered_count": len(registered),
                "elapsed_seconds": elapsed,
                "error": error,
                "board_none": board is None,
                "response_keys": board.get("response_keys") if board else None,
                "current_price": board.get("current_price") if board else None,
                "trading_volume": board.get("raw_trading_volume") if board else None,
                "trading_value": board.get("raw_trading_value") if board else None,
                "trading_volume_time": board.get("trading_volume_time") if board else None,
                "vwap": board.get("vwap") if board else None,
            })
    return rows


def summarize(rows: list[dict]) -> list[dict]:
    """銘柄ごとに1回目と2回目の売買高・売買代金が変わったかを判定する。"""
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    summary = []
    for symbol, items in by_symbol.items():
        first, second = items[0], items[1] if len(items) > 1 else None
        summary.append({
            "symbol": symbol,
            "first_seq": first["seq"],
            "first_volume": first["trading_volume"],
            "first_value": first["trading_value"],
            "second_volume": second["trading_volume"] if second else None,
            "second_value": second["trading_value"] if second else None,
            "volume_changed": bool(second) and first["trading_volume"] != second["trading_volume"],
            "value_changed": bool(second) and first["trading_value"] != second["trading_value"],
        })
    return summary


def format_table(rows: list[dict]) -> str:
    header = f"{'seq':>4} {'symbol':<8} {'try':>3} {'reg':>4} {'sec':>8} {'price':>10} {'volume':>12} {'value':>16} err"
    lines = [header]
    for r in rows:
        lines.append(
            f"{r['seq']:>4} {r['symbol']:<8} {r['attempt']:>3} {r['registered_count']:>4} "
            f"{r['elapsed_seconds']:>8.1f} {str(r['current_price']):>10} {str(r['trading_volume']):>12} "
            f"{str(r['trading_value']):>16} {r['error'] or ('板None' if r['board_none'] else '')}"
        )
    return "\n".join(lines)


def main(argv=None, client=None, now=None) -> int:
    args = _parse_args(argv)
    if not args.force and client is None:
        with market_workflow_lock() as acquired:
            if not acquired:
                print("[警告] 他の市場処理が実行中のため中止します。--force で強制実行できます。")
                return 1
    if client is None:
        token = args.token or get_token_provider().peek_token()
        if not token:
            print("[エラー] 共有済みトークンがありません(トークンは新規発行しません)。--token で指定してください。")
            return 1
        client = BoardRepository(token)
    symbols = _load_symbols(args.symbols)
    if not symbols:
        print("[エラー] 対象銘柄がありません")
        return 1

    now = now or datetime.now(JST)
    in_session = is_trading_session(
        now, config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE,
        config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE,
    )
    print(f"[情報] 実行時刻(JST): {now.isoformat(timespec='seconds')} / {'場中' if in_session else '場外'}")
    if len(symbols) >= REGISTRATION_LIMIT - 5:
        print(f"[警告] 取得銘柄数が{len(symbols)}件で、登録上限{REGISTRATION_LIMIT}に近づきます(登録解除は行いません)。")

    rows = run_diagnosis(client, symbols, max(1, args.repeat), args.interval_seconds)
    print(format_table(rows))
    summary = summarize(rows)
    print(f"[情報] 1回目と2回目で売買高が変化: {sum(s['volume_changed'] for s in summary)}件 / "
          f"売買代金が変化: {sum(s['value_changed'] for s in summary)}件 / 全{len(summary)}銘柄")

    output = args.output or OUTPUT_DIRECTORY / f"board_turnover_diagnosis_{now.strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"generated_at": now.isoformat(), "in_session": in_session, "rows": rows, "summary": summary},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[情報] JSON保存: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
