"""
================================================================================
分足バックフィル・エントリーポイント
過去N日分のフィルタリング結果に登場した銘柄について、Yahoo Financeの
1分足を取得し、自前ポーリングで貯めた分足データを上書き補強します。

想定運用：毎週月曜のバックテスト実行前に1回だけ実行する
（Yahooの1分足は直近7日程度しか遡れないため、週次で回収しておく）。
================================================================================
"""
import argparse
import logging
from datetime import date, datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from src.entrypoints.run_backtest import load_daily_filtering_symbols
from src.application.minute_bar_backfill_usecase import MinuteBarBackfillUseCase
from src.config import config
from src.infrastructure.market_data.get_intraday_bars import get_yahoo_intraday_bars
from src.infrastructure.notification.line_notify import process_notification, send_line_notify
from src.infrastructure.persistence.minute_bar_repository import MinuteBarRepository

logger = logging.getLogger(__name__)


def collect_recent_symbols(filtering_dir: Path, days: int, today: date) -> list[str]:
    """直近days日分のフィルタリング結果に登場した銘柄を重複なく集めます。"""
    if not filtering_dir.exists():
        return []
    daily_symbols = load_daily_filtering_symbols(filtering_dir)
    cutoff = today - timedelta(days=days)
    recent_symbols = {
        symbol
        for date_text, symbols_on_day in daily_symbols.items()
        if date.fromisoformat(date_text) >= cutoff
        for symbol in symbols_on_day
    }
    return sorted(recent_symbols)


def configure_logging() -> None:
    """標準出力と日次ローテーションするファイルへログを出力します。"""
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    file_handler = TimedRotatingFileHandler(
        config.LOG_FILE_PATH,
        when='midnight',
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


def main(now_provider=None, filtering_dir: Path | None = None, output_dir: Path | None = None) -> None:
    configure_logging()
    now = (now_provider or datetime.now)()

    parser = argparse.ArgumentParser(description="Yahoo分足で自前収集データをバックフィルします")
    parser.add_argument("--days", type=int, default=7, help="遡って取得する日数（Yahoo 1分足の実用上限は7日程度）")
    parser.add_argument("--filtering-dir", type=Path, default=None, help="フィルタリング結果ディレクトリ")
    parser.add_argument("--output-dir", type=Path, default=None, help="分足保存先ディレクトリ")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    target_filtering_dir = filtering_dir or args.filtering_dir or (project_root / "data" / "filtering")
    target_output_dir = output_dir or args.output_dir or (project_root / "data" / "minute_bars")

    symbols = collect_recent_symbols(target_filtering_dir, args.days, now.date())
    if not symbols:
        logger.info("直近%d日分のフィルタリング結果がないため、バックフィルを行いません。", args.days)
        return

    with process_notification("分足バックフィル", notify_lifecycle=False, trigger="フィルタリング結果"):
        usecase = MinuteBarBackfillUseCase(
            fetch_intraday_bars=get_yahoo_intraday_bars,
            repository=MinuteBarRepository(target_output_dir),
        )
        imported_counts = usecase.run(symbols, days=args.days)

        total = sum(imported_counts.values())
        send_line_notify(
            f"【分足バックフィル】結果\n対象銘柄数: {len(symbols)}\n取り込んだ足の総数: {total}"
        )


if __name__ == "__main__":
    main()
