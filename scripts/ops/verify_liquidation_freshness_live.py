"""
================================================================================
鮮度検証(TradingUseCase._fresh_liquidation_quote)の実API疎通確認スクリプト
フィルタ対象銘柄の板を実際に取得し、新しい鮮度検証ロジックを通して可否・理由を表示します。

読み取り専用の診断スクリプトです。
- 発注は行いません
- 状態ファイル・DB・ログへの書き込みは行いません（標準出力のみ）
- APIトークンは既存の取得関数を利用し、値そのものは出力しません

使い方:
    python scripts/ops/verify_liquidation_freshness_live.py
    python scripts/ops/verify_liquidation_freshness_live.py --force  # 取引プロセス稼働中でも強制実行

注意:
    このスクリプトはトークンを新規発行するため、取引プロセス稼働中に実行すると
    稼働中プロセスの既存トークンを無効化するおそれがあります。
    既存の排他ロック(market_workflow_lock)で他の市場処理が稼働中と判定した場合は中止します。
================================================================================
"""
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.board_repository import BoardRepository
from src.infrastructure.calendar.japanese_calendar import is_trading_session
from src.infrastructure.execution_lock import market_workflow_lock

JST = timezone(timedelta(hours=9))
FILTERING_DIR = PROJECT_ROOT / "data" / "filtering"
SAMPLE_SIZE = 5
LOW_VOLUME_COUNT = 2


def load_latest_filter_symbols() -> list[str]:
    """data/filtering配下の最新日付ファイルからフィルタ対象銘柄を読み込みます。"""
    files = sorted(FILTERING_DIR.glob("20*-*-*.json"))
    if not files:
        raise RuntimeError("data/filtering にフィルタ結果ファイルが見つかりません")
    latest = files[-1]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    symbols = payload.get("symbols") or []
    print(f"[情報] フィルタ対象読込元: {latest.name} ({len(symbols)}銘柄)")
    return symbols


def select_sample_symbols(boards: dict[str, dict]) -> list[str]:
    """出来高の少ない銘柄を1〜2件含めてサンプルを選定します。"""
    available = {sym: b for sym, b in boards.items() if b is not None}
    by_volume = sorted(
        available.items(),
        key=lambda item: (item[1].get("trading_volume") if item[1].get("trading_volume") is not None else -1),
    )
    low_volume_symbols = [sym for sym, _ in by_volume[:LOW_VOLUME_COUNT]]
    remaining = [sym for sym in available if sym not in low_volume_symbols]
    sample = low_volume_symbols + remaining[: max(0, SAMPLE_SIZE - len(low_volume_symbols))]
    return sample[:SAMPLE_SIZE]


class _FixedBoardClient:
    """既に取得済みの板をそのまま_fresh_liquidation_quoteへ渡すための固定クライアント。"""

    def __init__(self, board: dict):
        self._board = board

    def get_current_board_with_freshness(self, token, symbol):
        return self._board


def run_freshness_check(token: str, symbol: str, board: dict):
    """本物のTradingUseCase._fresh_liquidation_quoteに同じ板をそのまま通します。"""
    dummy = SimpleNamespace(
        token=token,
        board_client=_FixedBoardClient(board),
        _current_now=None,
    )
    price, reason, returned_board = TradingUseCase._fresh_liquidation_quote(dummy, symbol)
    return price, reason, returned_board


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="取引プロセス(他の市場処理)が稼働中でも強制的にトークンを発行して続行する",
    )
    args = parser.parse_args()

    if not args.force:
        with market_workflow_lock() as acquired:
            if not acquired:
                print(
                    "[警告] 他の市場処理(取引・フィルタリング・スクリーニング)が実行中のため中止します。"
                    "このスクリプトはトークンを新規発行するため、稼働中プロセスのトークンを無効化するおそれがあります。"
                    " --force で強制実行できます。"
                )
                return 1

    token = get_api_token()
    if not token:
        print("[エラー] APIトークンの取得に失敗しました（kabuステーション起動・認証設定を確認してください）")
        return 1

    now = datetime.now(JST)
    in_session = is_trading_session(
        now,
        config.MARKET_OPEN_HOUR,
        config.MARKET_OPEN_MINUTE,
        config.MARKET_CLOSE_HOUR,
        config.MARKET_CLOSE_MINUTE,
    )
    expected_status = 1 if in_session else 8
    print(f"[情報] 実行時刻(JST): {now.isoformat(timespec='seconds')}")
    print(f"[情報] 場中判定: {'場中' if in_session else '場外'} → 想定CurrentPriceStatus: {expected_status}")
    print()

    symbols = load_latest_filter_symbols()
    if not symbols:
        print("[エラー] フィルタ対象銘柄が空です")
        return 1

    repo = BoardRepository(token)
    boards: dict[str, dict] = {}
    for symbol in symbols:
        boards[symbol] = repo.get_current_board_with_freshness(symbol)

    sample_symbols = select_sample_symbols(boards)
    if not sample_symbols:
        print("[エラー] 板情報を取得できた銘柄がありません")
        return 1

    print(f"[情報] 検証対象サンプル: {sample_symbols}")
    print("=" * 80)

    for symbol in sample_symbols:
        board = boards.get(symbol)
        print(f"銘柄: {symbol}")
        if board is None:
            print("  板取得失敗: None")
            print("-" * 80)
            continue

        raw_time = board.get("current_price_time")
        parsed_jst = None
        try:
            parsed = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
            if parsed is not None:
                parsed_jst = parsed.astimezone(JST) if parsed.tzinfo else parsed
        except (TypeError, ValueError):
            parsed_jst = None

        print(f"  銘柄名            : {board.get('symbol_name')}")
        print(f"  CurrentPrice      : {board.get('current_price')}")
        print(f"  CurrentPriceTime  : {raw_time!r} (生値)")
        print(f"  CurrentPriceTime  : {parsed_jst.isoformat() if parsed_jst else '解析不可'} (パース後JST)")
        print(f"  CurrentPriceStatus: {board.get('current_price_status')}")
        print(f"  TradingVolume     : {board.get('trading_volume')}")

        price, reason, _ = run_freshness_check(token, symbol, board)
        if reason is None:
            print(f"  鮮度検証結果      : 可 (price={price})")
        else:
            print(f"  鮮度検証結果      : 否 (reason={reason})")
        print("-" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
