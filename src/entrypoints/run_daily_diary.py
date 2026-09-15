import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from src.infrastructure.analysis.diary_context import build_diary_context
from src.infrastructure.analysis.diary_writer import create_diary_writer
from src.infrastructure.persistence.storage import write_json

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="note.com向けの日次開発・トレード日記を生成します")
    parser.add_argument("--date", default=None, help="対象最終日（YYYY-MM-DD）。省略時は実行日")
    parser.add_argument("--days", type=int, default=1, help="対象日数")
    parser.add_argument("--reports", type=Path, default=Path("data/reports"))
    parser.add_argument("--backtests", type=Path, default=Path("data/backtest"))
    parser.add_argument("--notes", type=Path, default=Path("data/diary_notes"))
    parser.add_argument("--output", type=Path, default=Path("data/diary"))
    args = parser.parse_args()

    if args.days < 1:
        parser.error("--days は1以上を指定してください")
    end = date.fromisoformat(args.date) if args.date else date.today()
    start = end - timedelta(days=args.days - 1)
    context = build_diary_context(start, end, args.reports, args.backtests, args.notes)
    args.output.mkdir(parents=True, exist_ok=True)
    context_path = args.output / f"{end.isoformat()}.json"
    writer = create_diary_writer()
    if writer is None:
        logger.info("LLM未設定のためcontextのみ出力します")
        write_json(context_path, context)
        return

    diary = writer.write(context)
    if diary is None:
        logger.error("日記のLLM生成に失敗したためcontextのみ出力します")
        write_json(context_path, context)
        return

    output_path = args.output / f"{end.isoformat()}.md"
    output_path.write_text(f"# {end.isoformat()} 開発・トレード日記\n\n{diary}\n", encoding="utf-8")


if __name__ == "__main__":
    main()