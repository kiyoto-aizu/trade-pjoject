import logging
import subprocess
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from src.infrastructure.analysis import summary_loader

logger = logging.getLogger(__name__)


def _load_git_log(start: date, end: date) -> list[str]:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--since={start.isoformat()}",
                f"--until={(end + timedelta(days=1)).isoformat()}",
                "--pretty=format:%s",
            ],
            cwd=repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("gitログを読み込めません: %s", exc)
        return []
    return [line for line in (result.stdout or "").splitlines() if line]


def build_diary_context(
    start: date,
    end: date,
    report_directory: Path,
    backtest_directory: Path,
    notes_directory: Path,
    git_log_provider: Callable[[date, date], list[str]] | None = None,
) -> dict:
    manual_notes = []
    current_date = start
    while current_date <= end:
        note_path = notes_directory / f"{current_date.isoformat()}.md"
        try:
            if note_path.exists():
                manual_notes.append(f"{current_date.isoformat()}: {note_path.read_text(encoding='utf-8')}")
        except OSError as exc:
            logger.warning("手入力メモを読み込めません: %s (%s)", note_path, exc)
        current_date += timedelta(days=1)

    provider = git_log_provider or _load_git_log
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "commits": provider(start, end),
        "daily_reports": summary_loader.load_daily_summaries(report_directory, start, end),
        "backtest_runs": summary_loader.load_backtest_summaries(backtest_directory, start, end),
        "manual_notes": "\n\n".join(manual_notes),
    }