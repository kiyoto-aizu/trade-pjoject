import logging
from datetime import date
from pathlib import Path

from src.config import config
from src.config.task_schedule import ScheduledTask, tasks_for_date
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.notification.slack_notify import notify_critical, notify_daily

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def classify_task_run(
    task: ScheduledTask,
    target_date: date,
    log_text: str,
    reports_directory: Path,
) -> tuple[str, str]:
    start_marker = f"【{task.name}】開始"
    end_marker = f"【{task.name}】終了"
    error_marker = f"【{task.name}】異常終了"
    is_trading = task.name == "取引"
    has_report = (reports_directory / f"{target_date.isoformat()}.json").is_file()

    if error_marker in log_text:
        return "❌", "異常終了"
    if start_marker not in log_text:
        detail = "ログ記録なし・要確認"
        if is_trading:
            detail += "・" + ("日次レポートあり" if has_report else "日次レポートなし")
        return "⚠️", detail
    if end_marker not in log_text:
        return "⚠️", "開始記録のみ・終了記録なし（要確認）"
    if is_trading and not has_report:
        return "⚠️", "終了記録あり・日次レポートなし（要確認）"
    return "✅", ""


def build_task_check_message(
    target_date: date,
    log_path: Path | None = None,
    reports_directory: Path | None = None,
) -> tuple[str, bool]:
    log_file = log_path or Path(config.LOG_FILE_PATH)
    report_dir = reports_directory or PROJECT_ROOT / "data" / "reports" / "daily"
    try:
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log_text = ""
    log_text = "\n".join(
        line for line in log_text.splitlines()
        if line[:10] == target_date.isoformat()
    )

    tasks = tasks_for_date(target_date)
    heading = f"【本日のタスク実行結果】{target_date:%Y-%m-%d}({"月火水木金土日"[target_date.weekday()]})"
    if not tasks:
        return f"{heading}\n本日の実行予定タスクはありません。", False

    lines = [heading]
    needs_attention = False
    for task in tasks:
        status, detail = classify_task_run(task, target_date, log_text, report_dir)
        needs_attention = needs_attention or status != "✅"
        line = f"{status} {task.name} ({task.display_time})"
        if detail:
            line += f" {detail}"
        lines.append(line)
    return "\n".join(lines), needs_attention


def main(
    target_date: date | None = None,
    log_path: Path | None = None,
    reports_directory: Path | None = None,
) -> None:
    configure_logging()
    effective_date = target_date or date.today()
    message, needs_attention = build_task_check_message(
        effective_date, log_path=log_path, reports_directory=reports_directory,
    )
    logger.info("本日のタスク実行結果をSlackへ通知します。")
    notify_daily(message)
    if needs_attention:
        notify_critical(f"【本日のタスク実行要確認】\n{message}")


if __name__ == "__main__":
    main()