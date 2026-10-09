from datetime import date, datetime

import pytest

from src.entrypoints import run_midday_filtering
from src.infrastructure import execution_lock


@pytest.fixture
def lock_files(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_lock, "LOCK_FILE", tmp_path / ".market_workflow.lock")
    monkeypatch.setattr(execution_lock, "MIDDAY_FILTERING_LOCK_FILE", tmp_path / ".midday_filtering.lock")


def test_midday_lock_does_not_conflict_with_trading_lock(lock_files):
    with execution_lock.market_workflow_lock() as trading:
        assert trading is True
        with execution_lock.midday_filtering_lock() as midday:
            assert midday is True


def test_midday_lock_blocks_double_launch(lock_files):
    with execution_lock.midday_filtering_lock() as first:
        assert first is True
        with execution_lock.midday_filtering_lock() as second:
            assert second is False
    with execution_lock.midday_filtering_lock() as again:
        assert again is True


def test_market_workflow_lock_still_exclusive(lock_files):
    with execution_lock.market_workflow_lock() as first:
        assert first is True
        with execution_lock.market_workflow_lock() as second:
            assert second is False


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 9, 12, 0, 0)


def _run_main(monkeypatch, notices, *, trading_day=True, skip_dates=()):
    monkeypatch.setattr(run_midday_filtering, "configure_logging", lambda: None)
    monkeypatch.setattr(run_midday_filtering, "datetime", _FixedDatetime)
    monkeypatch.setattr(run_midday_filtering, "is_trading_day", lambda d: trading_day)
    monkeypatch.setattr(run_midday_filtering.config, "MIDDAY_FILTERING_SKIP_DATES", tuple(skip_dates))
    monkeypatch.setattr(run_midday_filtering, "notify_daily", notices.append)
    monkeypatch.setattr("sys.argv", ["run_midday_filtering"])
    run_midday_filtering.main()


def test_skip_notifies_on_holiday(monkeypatch):
    notices = []
    _run_main(monkeypatch, notices, trading_day=False)
    assert len(notices) == 1 and "休場日" in notices[0]


def test_skip_notifies_on_skip_date(monkeypatch):
    notices = []
    _run_main(monkeypatch, notices, skip_dates=[date(2026, 10, 9).isoformat()])
    assert len(notices) == 1 and "MIDDAY_FILTERING_SKIP_DATES" in notices[0]


def test_skip_notifies_when_midday_lock_is_held(lock_files, monkeypatch):
    notices = []
    with execution_lock.midday_filtering_lock() as held:
        assert held is True
        _run_main(monkeypatch, notices)
    assert len(notices) == 1 and "専用ロック" in notices[0]
