"""トレンド答え合わせ(日次・週次・月次)のCLI。判定・集計のみで、売買処理には関与しない。

使い方の例:
  python -m src.entrypoints.run_trend_check                         # 今日を最新版で判定(引け後)
  python -m src.entrypoints.run_trend_check --date 2026-10-06 --refresh-cache
  python -m src.entrypoints.run_trend_check --weekly                # 今週(--week-startで指定可)
  python -m src.entrypoints.run_trend_check --monthly --month 2026-10
  python -m src.entrypoints.run_trend_check --criteria-version v2 --reason "理由" --set open_to_close_min_pct=1.5
  python -m src.entrypoints.run_trend_check --recompute-all --criteria-version v2
"""
from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.application.trend_check_report import (
    month_bounds,
    week_bounds,
    write_daily_report,
    write_period_report,
    write_recompute_report,
)
from src.application.trend_check_usecase import (
    DEFAULT_DATABASE_FILE,
    DEFAULT_REPORT_DIRECTORY,
    TrendCheckPaths,
    ensure_builtin_versions,
    list_filtering_dates,
    recompute_all,
    refresh_daily_cache,
    run_day,
    symbols_for_date,
)
from src.infrastructure.persistence.trend_check_repository import TrendCheckRepository

logger = logging.getLogger(__name__)
PROCESS_NAME = "トレンド答え合わせ"


def _parse_overrides(items: list[str]) -> dict[str, float]:
    overrides = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"--setは キー=数値 の形式で指定してください: {item}")
        overrides[key.strip()] = float(value)
    return overrides


def register_new_version(
    repository: TrendCheckRepository, version: str, reason: str, start_date: str, overrides: dict[str, float]
) -> dict:
    """最新版の定義を土台に、しきい値を差し替えた新しい版を登録する(元の版は変更しない)。"""
    if repository.get_version(version) is not None:
        raise ValueError(f"版{version}は既に登録されています(別の版名を指定してください)。")
    base = repository.latest_version()
    definition = copy.deepcopy(base["definition"])
    unknown = sorted(set(overrides) - set(definition["thresholds"]))
    if unknown:
        raise ValueError(f"定義にないしきい値です: {', '.join(unknown)} (有効: {', '.join(definition['thresholds'])})")
    definition["thresholds"].update(overrides)
    definition["version"] = version
    definition["status"] = f"{base['version']}をもとに変更"
    repository.register_version(version, definition, start_date, reason)
    return definition


def _notify_failure(message: str) -> None:
    from src.infrastructure.notification.slack_notify import notify_process_end

    notify_process_end(PROCESS_NAME, success=False, detail=message)


def run_daily_safely(
    trade_date: date,
    refresh_cache: bool = True,
    database_file: Path = DEFAULT_DATABASE_FILE,
    output_dir: Path = DEFAULT_REPORT_DIRECTORY,
    paths: Optional[TrendCheckPaths] = None,
    notify_failure: Callable[[str], None] = _notify_failure,
) -> bool:
    """引け後の日次答え合わせ。失敗しても例外は外へ出さず、ログとSlack通知だけ行う(売買処理に影響させない)。"""
    try:
        paths = paths or TrendCheckPaths.default()
        day = trade_date.isoformat()
        if not (paths.filtering_dir / f"{day}.json").exists():
            logger.info("%s: フィルタ結果がないため答え合わせをスキップします: %s", PROCESS_NAME, day)
            return True
        repository = TrendCheckRepository(database_file)
        ensure_builtin_versions(repository, day)
        if refresh_cache:
            try:
                refresh_daily_cache(symbols_for_date(paths, day), paths)
            except Exception:
                logger.exception("%s: 日足キャッシュの更新に失敗しました(判定不能として保存します)", PROCESS_NAME)
        version = repository.latest_version()["version"]
        summary = run_day(repository, day, version, paths)
        write_daily_report(repository, day, output_dir, version)
        logger.info(
            "%s: %s 版=%s 選定トレンド=%s/%s 判定不能=%s", PROCESS_NAME, day, version,
            summary["selected_trend"], summary["selected_judged"], summary["selected_undecidable"],
        )
        return True
    except Exception as exc:
        logger.exception("%s: 日次の答え合わせに失敗しました", PROCESS_NAME)
        try:
            notify_failure(f"日次の答え合わせに失敗しました({trade_date}): {type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("%s: 失敗通知にも失敗しました", PROCESS_NAME)
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="トレンド答え合わせ(日次→週次→月次)を実行します")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="日次の対象日(省略時: 他の動作指定が無ければ今日)")
    parser.add_argument("--weekly", action="store_true", help="週次レポートを作る")
    parser.add_argument("--week-start", type=date.fromisoformat, default=None, help="対象週の月曜(省略時: 今週)")
    parser.add_argument("--monthly", action="store_true", help="月次レポートを作る")
    parser.add_argument("--month", default=None, help="対象月 YYYY-MM(省略時: 当月)")
    parser.add_argument("--criteria-version", default=None, help="使う判定基準の版。未登録の版は--reasonと共に新規登録")
    parser.add_argument("--reason", default=None, help="新しい版の変更理由")
    parser.add_argument("--set", dest="overrides", action="append", default=[], help="新しい版のしきい値変更 キー=数値(複数可)")
    parser.add_argument("--start-date", default=None, help="新しい版の開始日(省略時: 今日)")
    parser.add_argument("--recompute-all", action="store_true", help="保存済みの全フィルタ結果を元の日足・分足から再計算")
    parser.add_argument("--refresh-cache", action="store_true", help="判定前に対象銘柄の日足キャッシュを更新する")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_DIRECTORY)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = TrendCheckPaths.default()
    today = date.today()

    repository = TrendCheckRepository(args.db)
    filtering_dates = list_filtering_dates(paths)
    ensure_builtin_versions(repository, filtering_dates[0] if filtering_dates else today.isoformat())

    try:
        if args.criteria_version and repository.get_version(args.criteria_version) is None:
            if not args.reason:
                parser.error("新しい版の登録には --reason (変更理由)が必要です")
            register_new_version(
                repository, args.criteria_version, args.reason,
                args.start_date or today.isoformat(), _parse_overrides(args.overrides),
            )
            logger.info("判定基準 %s を登録しました。旧版の結果はそのまま残ります。", args.criteria_version)
        elif args.reason and args.criteria_version:
            parser.error(f"版{args.criteria_version}は登録済みです。--reasonは新規の版の登録時のみ指定できます")
    except ValueError as exc:
        parser.error(str(exc))

    version = args.criteria_version or repository.latest_version()["version"]
    explicit_action = args.recompute_all or args.weekly or args.monthly or args.date or args.reason
    did_something = False

    if args.recompute_all:
        summaries = recompute_all(repository, version, paths, progress=lambda day: logger.info("再計算: %s", day))
        for item in summaries:
            write_daily_report(repository, item["trade_date"], args.output, version)
        report_path = write_recompute_report(repository, version, summaries, args.output)
        logger.info("全期間の再計算が完了しました: %s日 / %s", len(summaries), report_path)
        did_something = True

    if args.date or not explicit_action:
        target = args.date or today
        if args.refresh_cache:
            refresh_daily_cache(symbols_for_date(paths, target.isoformat()), paths)
        summary = run_day(repository, target.isoformat(), version, paths)
        if summary is None:
            logger.error("対象日のフィルタ結果がありません: %s", target)
            return 1
        logger.info("日次レポート: %s", write_daily_report(repository, target.isoformat(), args.output, version))
        did_something = True

    if args.weekly:
        start, end = week_bounds(args.week_start or today)
        path = write_period_report(repository, "weekly", start, end, f"{start}_{end}", args.output)
        logger.info("週次レポート: %s", path or "対象期間に日次結果がありません")
        did_something = True

    if args.monthly:
        month_text = args.month or f"{today.year:04d}-{today.month:02d}"
        start, end = month_bounds(month_text)
        path = write_period_report(repository, "monthly", start, end, month_text, args.output)
        logger.info("月次レポート: %s", path or "対象期間に日次結果がありません")
        did_something = True

    if not did_something:
        logger.info("版の登録のみ行いました。再計算は --recompute-all --criteria-version %s で実行できます。", version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
