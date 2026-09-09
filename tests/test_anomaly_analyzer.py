from datetime import datetime, timedelta

from src.application.filtering_usecase import FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.config import config
from src.domain.enums import RankingType
from src.domain.models import RankingEntry, Regulation, ScreeningResult
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


class RankingStub:
    def get_ranking(self, ranking_type, exchange_division="ALL"):
        return [RankingEntry("7203", 1, 100.0, ranking_type, 100.0)]


class RegulationStub:
    def get_regulation(self, symbol, market_code):
        return Regulation(symbol, False)


class ExchangeStub:
    def get_primary_exchange(self, symbol):
        return 1


class BoardStub:
    def get_current_board(self, symbol):
        return {"current_price": 100, "trading_volume": 10}


class VolumeStub:
    def get_average_volume(self, symbol, days):
        return 5


def _stub_llm(monkeypatch, content="観測された異常\n- 採用件数が普段より少ないです。"):
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(
        "src.infrastructure.analysis.anomaly_analyzer.requests.post",
        lambda url, **kwargs: DummyResponse(),
    )


def test_screening_usecase_adds_llm_anomaly_note_when_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_ANOMALY_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "dummy-key")
    monkeypatch.setattr(config, "SCREENING_ANOMALY_MIN_SYMBOLS", 5)
    _stub_llm(monkeypatch)

    notifications = []
    ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(),
        ScreeningResultRepository(tmp_path), notifications.append,
    ).execute()

    assert "--- LLM異常検知（参考） ---" in notifications[0]
    assert "採用件数が普段より少ない" in notifications[0]


def test_filtering_usecase_adds_llm_anomaly_note_when_below_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_ANOMALY_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "dummy-key")
    monkeypatch.setattr(config, "FILTERING_ANOMALY_MIN_SYMBOLS", 3)
    _stub_llm(monkeypatch)

    today = datetime.now().date()
    previous_business_day = today - timedelta(days=1)
    while previous_business_day.weekday() >= 5:
        previous_business_day -= timedelta(days=1)
    screening_repository = ScreeningResultRepository(tmp_path / "screening")
    screening_repository.save(ScreeningResult(
        previous_business_day.isoformat(), ["7203"], datetime.now().isoformat()
    ))

    notifications = []
    FilteringUseCase(
        screening_repository, BoardStub(), VolumeStub(),
        FilteringResultRepository(tmp_path / "filtering"), notifications.append,
    ).execute()

    assert "--- LLM異常検知（参考） ---" in notifications[0]
    assert "採用件数が普段より少ない" in notifications[0]


def test_screening_usecase_skips_llm_when_above_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_ANOMALY_ANALYSIS_ENABLED", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "dummy-key")
    monkeypatch.setattr(config, "SCREENING_ANOMALY_MIN_SYMBOLS", 1)
    calls = []
    monkeypatch.setattr(
        "src.infrastructure.analysis.anomaly_analyzer.requests.post",
        lambda url, **kwargs: calls.append(url),
    )

    notifications = []
    ScreeningUseCase(
        RankingStub(), RegulationStub(), ExchangeStub(),
        ScreeningResultRepository(tmp_path), notifications.append,
    ).execute()

    assert calls == []
    assert "LLM異常検知" not in notifications[0]
