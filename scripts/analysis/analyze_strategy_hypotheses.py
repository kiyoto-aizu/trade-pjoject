"""保存済みの分析結果を使って戦略仮説レポートを生成する読み取り専用ツール。

実行方法:
    python scripts/analysis/analyze_strategy_hypotheses.py --start-date 2026-09-21 --end-date 2026-09-25
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application.generate_strategy_hypotheses_usecase import GenerateStrategyHypothesesUseCase  # noqa: E402
from src.infrastructure.analysis.strategy_hypothesis_analyzer import (  # noqa: E402
    create_strategy_hypothesis_analyzer,
)
from src.infrastructure.analysis.strategy_hypothesis_context_repository import (  # noqa: E402
    StrategyHypothesisContextRepository,
    StrategyHypothesisReportRepository,
)

logger = logging.getLogger(__name__)


def _parse_date(value: str) -> date:
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("YYYY-MM-DD形式の日付を指定してください") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="既存の分析結果から戦略仮説レポートを生成します")
    parser.add_argument("--start-date", required=True, type=_parse_date, help="対象期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=True, type=_parse_date, help="対象期間の終了日 (YYYY-MM-DD)")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "strategy_hypotheses")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("--start-dateは--end-date以前の日付を指定してください")

    analyzer = create_strategy_hypothesis_analyzer()
    if analyzer is None:
        parser.error("LLM分析が無効かAPIキーが未設定です。既存のLLM設定を確認してください")

    context_repository = StrategyHypothesisContextRepository(
        weekly_directory=PROJECT_ROOT / "data" / "reports" / "weekly",
        monthly_directory=PROJECT_ROOT / "data" / "reports" / "monthly",
        analysis_directory=PROJECT_ROOT / "data" / "analysis",
        adr_directory=PROJECT_ROOT / "docs" / "adr",
        verification_directory=PROJECT_ROOT / "data" / "strategy_verification",
    )
    use_case = GenerateStrategyHypothesesUseCase(
        context_repository=context_repository,
        analyzer=analyzer,
        report_repository=StrategyHypothesisReportRepository(args.output_dir),
    )
    try:
        result = use_case.execute(args.start_date, args.end_date)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("戦略仮説レポートを生成できませんでした: %s", exc)
        raise SystemExit(1) from exc

    print(result["markdown"])
    print(f"\n保存先: {result['output_path']}")


if __name__ == "__main__":
    main()