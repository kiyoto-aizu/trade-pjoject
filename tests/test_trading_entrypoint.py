from datetime import date, datetime

from src.config import config
from src.entrypoints import run_trading
from src.infrastructure.calendar.japanese_calendar import is_trading_day, is_trading_session


def test_is_trading_session_accepts_weekday_market_hours():
    assert is_trading_session(datetime(2026, 9, 4, 9, 0), 9, 0, 15, 30)
    assert is_trading_session(datetime(2026, 9, 4, 15, 29), 9, 0, 15, 30)


def test_is_trading_session_rejects_boundaries_and_weekends():
    assert not is_trading_session(datetime(2026, 9, 4, 8, 59), 9, 0, 15, 30)
    assert not is_trading_session(datetime(2026, 9, 4, 15, 30), 9, 0, 15, 30)
    assert not is_trading_session(datetime(2026, 9, 5, 10, 0), 9, 0, 15, 30)


def test_is_trading_session_rejects_holiday_on_a_weekday():
    # 2026-09-21は敬老の日（月曜）
    assert not is_trading_session(datetime(2026, 9, 21, 10, 0), 9, 0, 15, 30)


def test_is_trading_day_accepts_ordinary_weekday():
    assert is_trading_day(date(2026, 9, 4))


def test_is_trading_day_rejects_weekend():
    assert not is_trading_day(date(2026, 9, 5))  # 土曜
    assert not is_trading_day(date(2026, 9, 6))  # 日曜


def test_is_trading_day_rejects_national_holiday():
    assert not is_trading_day(date(2026, 9, 21))  # 敬老の日（月曜）


def test_is_trading_day_rejects_year_end_new_year():
    assert not is_trading_day(date(2026, 12, 31))
    assert not is_trading_day(date(2026, 1, 1))  # 元日（祝日カレンダー経由）
    assert not is_trading_day(date(2026, 1, 2))
    assert not is_trading_day(date(2026, 1, 3))


def test_main_does_not_request_token_outside_trading_session(monkeypatch):
    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(
        run_trading,
        'get_api_token',
        lambda: (_ for _ in ()).throw(AssertionError('token was requested')),
    )

    run_trading.main(now_provider=lambda: datetime(2026, 9, 5, 10, 0))


def test_main_can_enter_the_pre_close_liquidation_path(monkeypatch):
    class FilteringResult:
        symbols = ['7203']

    class FilteringRepository:
        def __init__(self, path):
            pass

        def load_for_date(self, target_date):
            return FilteringResult()

    class Bot:
        def __init__(self, token):
            assert token == 'dummy'
            self.was_run = False

        def run(self, **kwargs):
            self.was_run = True

    class NoOpContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_trading, 'FilteringResultRepository', FilteringRepository)
    monkeypatch.setattr(run_trading, 'create_trading_use_case', Bot)
    monkeypatch.setattr(run_trading, 'get_api_token', lambda: 'dummy')
    monkeypatch.setattr(run_trading, 'market_workflow_lock', NoOpContext)
    monkeypatch.setattr(run_trading, 'process_notification', lambda *args, **kwargs: NoOpContext())
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', False)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)

    run_trading.main(now_provider=lambda: datetime(2026, 9, 4, 15, 20))


def test_main_notifies_the_actual_monitored_symbol_count(monkeypatch):
    class FilteringResult:
        symbols = ['7203', '6758', '8306', '9432', '9984', '8058']

    class FilteringRepository:
        def __init__(self, path):
            pass

        def load_for_date(self, target_date):
            return FilteringResult()

    class Bot:
        def __init__(self, token):
            assert token == 'dummy'

        def prepare_market_regime(self):
            pass

        def market_conditions_detail(self):
            return ['市場レジーム: NORMAL']

        def collect_preflight_market_data(self, symbols):
            return {}

        def run(self, **kwargs):
            pass

    class NoOpContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    notifications = []
    monkeypatch.setattr(run_trading, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_trading, 'FilteringResultRepository', FilteringRepository)
    monkeypatch.setattr(run_trading, 'create_trading_use_case', Bot)
    monkeypatch.setattr(run_trading, 'get_api_token', lambda: 'dummy')
    monkeypatch.setattr(run_trading, 'market_workflow_lock', NoOpContext)
    monkeypatch.setattr(run_trading, 'process_notification', lambda *args, **kwargs: NoOpContext())
    monkeypatch.setattr(run_trading, 'notify_daily', notifications.append)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)

    run_trading.main(now_provider=lambda: datetime(2026, 9, 4, 10, 0))

    assert notifications
    assert '対象銘柄数: 6' in notifications[0]
    assert '市場レジーム: NORMAL' in notifications[0]
