"""戦略仮説レポートを読み取り専用データ分析で検証する手動実行ツール。

実行方法:
    python scripts/analysis/verify_strategy_hypotheses.py data/strategy_hypotheses/2026-09-21_2026-09-25.md
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application.verify_strategy_hypotheses_usecase import VerifyStrategyHypothesesUseCase  # noqa: E402
from src.config import config  # noqa: E402
from src.infrastructure.analysis.strategy_analysis_code_sandbox import StrategyAnalysisCodeSandbox  # noqa: E402
from src.infrastructure.analysis.strategy_verification_client import (  # noqa: E402
    create_strategy_verification_client,
)
from src.infrastructure.notification.slack_notify import (  # noqa: E402
    format_result_notification,
    notify_analysis,
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
    parser = argparse.ArgumentParser(description="戦略仮説レポートを定量データで検証します")
    parser.add_argument(
        "advisor_report",
        type=Path,
        nargs="?",
        help="analyze_strategy_hypotheses.pyが生成したMarkdown",
    )
    parser.add_argument("--hypothesis-file", type=Path, help="特定仮説を含む既存Markdownレポート")
    parser.add_argument("--hypothesis-id", help="レポート内の仮説番号 (例: 2)")
    parser.add_argument("--adhoc", help="アドバイザーを介さず検証する仮説・気づき")
    parser.add_argument(
        "--replay-code",
        type=Path,
        help="監査ディレクトリ内のanalysis_code.pyをLLM再呼出しなしで再実行",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "strategy_verification",
        help="監査ログ・検証フィードバックの保存先",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="仮説ごとの生成コード実行タイムアウト秒数 (default: 60)",
    )
    parser.add_argument("--start-date", type=_parse_date, help="アドホック調査期間の開始日 (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=_parse_date, help="アドホック調査期間の終了日 (YYYY-MM-DD)")
    return parser


def _notify_standalone_result(result: dict) -> None:
    verification = result["results"]
    details = [
        f"検証結果: {result['run_directory']}",
        f"フィードバック: {result['feedback_path']}",
    ]
    for item in verification:
        details.extend([
            f"{item['title']}: {item['verdict']} (信頼度: {item['confidence']})",
            f"理由: {item['reason'][:500]}",
        ])
    notification = format_result_notification(
        "分析運用",
        "仮説検証役",
        f"{len(verification)}件の仮説検証が終了しました。",
        details,
    )
    if not notify_analysis(notification):
        logger.warning("単体仮説検証のSlack通知に失敗しました")


def _notify_standalone_failure(error: Exception) -> None:
    notification = format_result_notification(
        "分析運用",
        "仮説検証役",
        "単体仮説検証を完了できませんでした。",
        [f"エラー種別: {type(error).__name__}", str(error)[:500]],
    )
    if not notify_analysis(notification):
        logger.warning("単体仮説検証の失敗Slack通知に失敗しました")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    modes = (
        args.advisor_report is not None,
        args.hypothesis_file is not None or args.hypothesis_id is not None,
        args.adhoc is not None,
        args.replay_code is not None,
    )
    if sum(modes) != 1:
        parser.error("レポート全件、--hypothesis-file/--hypothesis-id、--adhoc、--replay-codeのいずれか一つを指定してください")
    if (args.hypothesis_file is None) != (args.hypothesis_id is None):
        parser.error("--hypothesis-fileと--hypothesis-idは両方指定してください")
    if args.hypothesis_id is not None and not args.hypothesis_id.isdigit():
        parser.error("--hypothesis-idは数字を指定してください")
    if args.advisor_report is not None and not args.advisor_report.is_file():
        parser.error(f"仮説レポートが見つかりません: {args.advisor_report}")
    if args.hypothesis_file is not None and not args.hypothesis_file.is_file():
        parser.error(f"仮説レポートが見つかりません: {args.hypothesis_file}")
    if args.replay_code is not None and not args.replay_code.is_file():
        parser.error(f"再実行コードが見つかりません: {args.replay_code}")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-secondsは正数を指定してください")
    if (args.start_date is None) != (args.end_date is None):
        parser.error("--start-dateと--end-dateは両方指定してください")
    if args.start_date is not None and args.start_date > args.end_date:
        parser.error("--start-dateは--end-date以前の日付を指定してください")
    if args.start_date is not None and args.adhoc is None:
        parser.error("--start-date/--end-dateは--adhocでのみ指定できます")
    needs_client = args.replay_code is None
    client = create_strategy_verification_client() if needs_client else None
    if needs_client and client is None:
        parser.error("LLM分析が無効かAPIキーが未設定です。既存のLLM設定を確認してください")

    sandbox = StrategyAnalysisCodeSandbox(
        parquet_directory=PROJECT_ROOT / "data" / "minute_bars_parquet",
        sqlite_path=config.FILTER_DECISION_DATABASE_FILE,
        timeout_seconds=args.timeout_seconds,
    )
    use_case = VerifyStrategyHypothesesUseCase(client, sandbox, args.output_dir)
    try:
        if args.replay_code is not None:
            result = use_case.replay_saved_code(args.replay_code)
        elif args.hypothesis_file is not None:
            result = use_case.execute_selected(args.hypothesis_file, args.hypothesis_id)
            _notify_standalone_result(result)
        elif args.adhoc is not None:
            end = args.end_date or date.today()
            start = args.start_date or end - timedelta(days=6)
            result = use_case.execute_adhoc(args.adhoc, start, end)
            _notify_standalone_result(result)
        else:
            result = use_case.execute(args.advisor_report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("仮説検証を開始できませんでした: %s", exc)
        if args.hypothesis_file is not None or args.adhoc is not None:
            _notify_standalone_failure(exc)
        raise SystemExit(1) from exc

    print(result["markdown"])
    if args.replay_code is not None:
        print(f"\n再実行ログ: {result['replay_directory']}")
    elif args.hypothesis_file is not None or args.adhoc is not None:
        print(f"\n監査ログ: {result['run_directory']}")
        print(f"フィードバック: {result['feedback_path']}")
    else:
        print(f"\n監査ログ: {result['run_directory']}")
        print(f"フィードバック: {result['feedback_path']}")


if __name__ == "__main__":
    main()