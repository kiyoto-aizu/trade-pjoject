import logging
from datetime import date

from src.config.task_schedule import tasks_for_date
from src.infrastructure.logging_config import configure_logging
from src.infrastructure.notification.slack_notify import notify_daily

logger = logging.getLogger(__name__)
WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")


def build_task_plan_message(target_date: date) -> str:
    tasks = tasks_for_date(target_date)
    heading = f"【本日の実行予定タスク】{target_date:%Y-%m-%d}({WEEKDAYS_JA[target_date.weekday()]})"

    if not tasks and target_date.weekday() < 5:
        return f"{heading}\n本日は休場日のため、フィルタリング/取引/スクリーニングはスキップされます。"
    if not tasks:
        return f"{heading}\n本日の実行予定タスクはありません。"

    lines = [heading]
    for task in tasks:
        name = f"{task.name}(翌営業日向け)" if task.name == "スクリーニング" else task.name
        lines.append(f"{task.display_time} {name}")
    return "\n".join(lines)


def main(target_date: date | None = None) -> None:
    configure_logging()
    message = build_task_plan_message(target_date or date.today())
    logger.info("本日の実行予定をSlackへ通知します。")
    notify_daily(message)


if __name__ == "__main__":
    main()