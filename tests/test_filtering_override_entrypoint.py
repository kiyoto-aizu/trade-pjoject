import sys
from datetime import date

from src.entrypoints import run_filtering_override
from src.domain.models import ScreeningResult


class _StubScreeningRepository:
    def __init__(self, result_by_date):
        self.result_by_date = result_by_date
        self.requested_dates = []

    def load_for_date(self, target_date):
        self.requested_dates.append(target_date)
        return self.result_by_date.get(target_date)


def test_fixed_date_screening_repository_ignores_requested_date():
    inner = _StubScreeningRepository({
        date(2026, 9, 18): ScreeningResult('2026-09-18', ['1234'], '2026-09-18T15:35:00'),
    })
    wrapper = run_filtering_override.FixedDateScreeningRepository(inner, date(2026, 9, 18))

    # 呼び出し側がどんな日付を渡しても、常に9/18のスクリーニング結果を返す
    result = wrapper.load_for_date(date(2026, 9, 23))

    assert result.symbols == ['1234']
    assert inner.requested_dates == [date(2026, 9, 18)]


def test_main_uses_live_board_and_fixed_screening_date(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['run_filtering_override.py', '--screening-date', '2026-09-18'])
    monkeypatch.setattr(run_filtering_override, 'configure_logging', lambda: None)
    monkeypatch.setattr(run_filtering_override, 'get_api_token', lambda: 'dummy-token')
    monkeypatch.setattr(run_filtering_override, 'unregister_all', lambda token: True)

    captured = {}

    class _FakeUseCase:
        def __init__(self, screening_repository, board_client, volume_client, result_repository, notifier):
            captured['screening_repository'] = screening_repository
            captured['board_client'] = board_client
            captured['notifier'] = notifier

        def execute(self, target_date=None):
            captured['target_date'] = target_date
            return None

    monkeypatch.setattr(run_filtering_override, 'FilteringUseCase', _FakeUseCase)

    run_filtering_override.main()

    # ライブ板情報を使う分岐(target_date=None)で実行されること
    assert captured['target_date'] is None
    assert captured['board_client'] is not None
    assert isinstance(captured['screening_repository'], run_filtering_override.FixedDateScreeningRepository)
    assert captured['screening_repository'].screening_date == date(2026, 9, 18)
