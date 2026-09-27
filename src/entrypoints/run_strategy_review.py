from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application.generate_strategy_hypotheses_usecase import GenerateStrategyHypothesesUseCase  # noqa: E402
from src.application.run_strategy_review_usecase import RunStrategyReviewUseCase  # noqa: E402
from src.application.verify_strategy_hypotheses_usecase import VerifyStrategyHypothesesUseCase  # noqa: E402
from src.config import config  # noqa: E402
from src.infrastructure.analysis.strategy_analysis_code_sandbox import StrategyAnalysisCodeSandbox  # noqa: E402
from src.infrastructure.analysis.strategy_hypothesis_analyzer import (  # noqa: E402
    create_strategy_hypothesis_analyzer,
)
from src.infrastructure.analysis.strategy_hypothesis_context_repository import (  # noqa: E402
    StrategyHypothesisContextRepository,
    StrategyHypothesisReportRepository,
)
from src.infrastructure.analysis.daily_analyzer import create_daily_analyzer  # noqa: E402
from src.infrastructure.analysis.strategy_verification_client import (  # noqa: E402
    create_strategy_verification_client,
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
    parser = argparse.ArgumentParser(description="戦略仮説の生成と定量検証を連続実行します")
    parser.add_argument("--start-date", required=True, type=_parse_date, help="対象期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", required=True, type=_parse_date, help="対象期間の終了日 (YYYY-MM-DD)")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="仮説ごとの分析コード実行タイムアウト秒数 (default: 60)",
    )
    return parser


def _build_operations(
    timeout_seconds: float,
) -> tuple[Callable[[date, date], dict], Callable[[Path], dict]]:
    def generate_hypotheses(start: date, end: date) -> dict:
        analyzer = create_strategy_hypothesis_analyzer()
        if analyzer is None:
            raise RuntimeError("LLM分析が無効かAPIキーが未設定です")
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
            report_repository=StrategyHypothesisReportRepository(
                PROJECT_ROOT / "data" / "strategy_hypotheses"
            ),
        )
        return use_case.execute(start, end)

    def verify_hypotheses(advisor_path: Path) -> dict:
        client = create_strategy_verification_client()
        if client is None:
            raise RuntimeError("LLM分析が無効かAPIキーが未設定です")
        use_case = VerifyStrategyHypothesesUseCase(
            client=client,
            sandbox=StrategyAnalysisCodeSandbox(
                parquet_directory=PROJECT_ROOT / "data" / "minute_bars_parquet",
                sqlite_path=config.FILTER_DECISION_DATABASE_FILE,
                timeout_seconds=timeout_seconds,
            ),
            output_directory=PROJECT_ROOT / "data" / "strategy_verification",
        )
        return use_case.execute(advisor_path)

    return generate_hypotheses, verify_hypotheses


def _build_summarizer() -> Callable[[dict], str | None] | None:
    analyzer = create_daily_analyzer()
    return analyzer.analyze_strategy_review if analyzer is not None else None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("--start-dateは--end-date以前の日付を指定してください")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-secondsは正数を指定してください")

    try:
        advisor, verifier = _build_operations(args.timeout_seconds)
        use_case = RunStrategyReviewUseCase(
            advisor=advisor,
            verifier=verifier,
            summarizer=_build_summarizer(),
        )
        result = use_case.execute(args.start_date, args.end_date)
    except Exception as exc:
        logger.exception("戦略レビューに失敗しました")
        if isinstance(exc, (argparse.ArgumentError, SystemExit)):
            raise
        raise SystemExit(1) from exc
    print(result["notification"])


if __name__ == "__main__":
    main()