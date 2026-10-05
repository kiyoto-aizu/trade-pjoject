from datetime import date, datetime

import pytest

from src.application.filtering_usecase import FilteringUseCase
from src.application.screening_usecase import ScreeningUseCase
from src.application.trading_usecase import TradingUseCase
from src.domain.enums import RankingType
from src.domain.market_regime import MarketRegime, MarketRegimeAssessment
from src.domain.models import FilteringResult, RankingEntry, Regulation, ScreeningResult
from src.domain.market_tendency import TendencyPeriod
from src.application.market_tendency_notification import build_market_tendency_lines
from src.entrypoints import run_trading
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


def market_assessment(regime=MarketRegime.NORMAL):
    return MarketRegimeAssessment(
        regime=regime,
        realized_volatility_percent=18.0,
        vix=16.0,
        nikkei_change_percent=-0.5,
        data_available=True,
        adx=20.0,
    )


class MarketRegimeStub:
    def __init__(self, assessment):
        self.assessment = assessment

    def execute(self):
        return self.assessment


def test_screening_notification_includes_previous_close_tendency_and_candidate_activity(monkeypatch, tmp_path):
    class Ranking:
        def get_ranking(self, ranking_type, exchange_division="ALL", target_date=None):
            return [RankingEntry("7203", 1, 100_000.0, ranking_type, 100.0)]

    class Regulations:
        def get_regulation(self, symbol, market_code, target_date=None):
            return Regulation(symbol, False)

    class Exchanges:
        def get_primary_exchange(self, symbol):
            return 1

    class Turnover:
        def get_average_turnover_before(self, symbol, target_date, days):
            assert days == 20
            return 200_000.0

    monkeypatch.setattr("src.application.screening_usecase.sleep", lambda _seconds: None)
    notifications = []
    ScreeningUseCase(
        Ranking(),
        Regulations(),
        Exchanges(),
        ScreeningResultRepository(tmp_path),
        notifications.append,
        market_regime_usecase=MarketRegimeStub(market_assessment()),
        turnover_client=Turnover(),
    ).execute(target_date=date(2026, 10, 5))

    message = notifications[0]
    assert "地合い" not in message
    assert "傾向(" not in message
    assert "候補の活発度: 20日平均売買代金比 最大0.5倍・平均0.5倍(動きは小さめ)" in message


def test_filtering_notification_uses_selected_surge_ratios(tmp_path):
    target_date = date(2026, 10, 5)

    class Screening:
        def load_for_date(self, requested_date):
            return ScreeningResult("2026-10-02", ["7203"], "2026-10-02T15:00:00")

    class Board:
        def get_current_board(self, symbol):
            raise AssertionError("過去日再計算では板を参照しない")

    class Turnover:
        def get_turnover_for_date(self, symbol, requested_date):
            assert requested_date == target_date
            return 500_000.0

        def get_average_turnover_before(self, symbol, requested_date, days):
            assert requested_date == target_date
            return 1_000_000.0

    notifications = []
    usecase = FilteringUseCase(
        Screening(),
        Board(),
        Turnover(),
        FilteringResultRepository(tmp_path),
        notifications.append,
        market_regime_usecase=MarketRegimeStub(market_assessment()),
    )
    result = usecase.execute(target_date=target_date)

    assert result.symbols == ["7203"]
    message = notifications[0]
    assert "地合い" not in message
    assert "傾向(" not in message
    assert "通過銘柄の活発度: 20日平均売買代金比 最大0.5倍・平均0.5倍(動きは小さめ)" in message


@pytest.mark.parametrize(
    ("regime", "nikkei", "realized_volatility", "vix", "adx", "expected_lines"),
    [
        (
            MarketRegime.DANGER,
            3.30,
            23.35,
            16.39,
            17.41,
            [
                "MarketRegime: DANGER（危険・新規買い停止）",
                "日経前日比: 3.30%（状態: 上昇）",
                "実現ボラティリティ: 23.35%（状態: 注意）",
                "VIX: 16.39（状態: 通常）",
                "ADX: 17.41（状態: 強いトレンドなし）",
                "傾向(前日終値ベース): 方向感は出にくく、値幅はやや出やすい地合い。上昇幅が大きく、振れが出やすい。",
                "今日の動き方: 新規買いは停止（保有銘柄の売却は継続）",
            ],
        ),
        (
            MarketRegime.NORMAL,
            0.30,
            12.00,
            14.00,
            30.00,
            [
                "MarketRegime: NORMAL（通常）",
                "日経前日比: 0.30%（状態: 上昇）",
                "実現ボラティリティ: 12.00%（状態: 通常）",
                "VIX: 14.00（状態: 通常）",
                "ADX: 30.00（状態: 強いトレンド）",
                "傾向(前日終値ベース): 方向感は出やすく、値幅は普通の地合い。地合いは堅調になりやすい。",
                "今日の動き方: 通常ルールで運用",
            ],
        ),
    ],
)
def test_trading_start_notification_for_danger_and_normal(
    monkeypatch, regime, nikkei, realized_volatility, vix, adx, expected_lines
):
    target_date = date(2026, 10, 5)
    assessment = MarketRegimeAssessment(
        regime=regime,
        realized_volatility_percent=realized_volatility,
        vix=vix,
        nikkei_change_percent=nikkei,
        data_available=True,
        adx=adx,
    )

    class FilteringRepository:
        def __init__(self, path):
            pass

        def load_for_date(self, requested_date):
            return FilteringResult(target_date.isoformat(), ["7203"], "2026-10-05T09:30:00")

    class Bot:
        market_regime = regime
        market_regime_assessment = assessment
        market_conditions_detail = TradingUseCase.market_conditions_detail

        def __init__(self, token):
            pass

        def warn_on_overnight_positions(self):
            pass

        def prepare_market_regime(self):
            pass

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
    monkeypatch.setattr(run_trading, "configure_logging", lambda: None)
    monkeypatch.setattr(run_trading, "FilteringResultRepository", FilteringRepository)
    monkeypatch.setattr(run_trading, "create_trading_use_case", Bot)
    monkeypatch.setattr(run_trading, "get_api_token", lambda: "dummy")
    monkeypatch.setattr(run_trading, "market_workflow_lock", NoOpContext)
    monkeypatch.setattr(run_trading, "process_notification", lambda *args, **kwargs: NoOpContext())
    monkeypatch.setattr(run_trading, "notify_daily", notifications.append)
    monkeypatch.setattr(run_trading, "_load_filtering_activity_ratios", lambda _date, _symbols: [0.7])

    run_trading.main(now_provider=lambda: datetime(2026, 10, 5, 10, 0))

    message = notifications[0]
    assert all(line in message for line in expected_lines)
    assert "地合い:" not in message
    assert "意味:" not in message
    assert "活発度" not in message


def test_unavailable_activity_is_logged_and_market_tendency_line_remains(caplog):
    lines = build_market_tendency_lines(
        market_assessment(),
        [None],
        period=TendencyPeriod.PREVIOUS_CLOSE,
        activity_label="候補の活発度",
    )

    assert any("地合い(前日終値ベース)" in line for line in lines)
    assert any("売買代金比を算出できません" in line for line in lines)
    assert "TENDENCY_ACTIVITY_UNAVAILABLE" in caplog.text


def test_build_market_tendency_lines_selects_lines_by_caller():
    lines = build_market_tendency_lines(
        market_assessment(),
        [0.5],
        period=TendencyPeriod.PREVIOUS_CLOSE,
        activity_label="候補の活発度",
        include_market_line=False,
        include_tendency_line=False,
    )

    assert len(lines) == 1
    assert lines[0].startswith("候補の活発度:")


def test_filtering_diagnostics_repository_loads_latest_date(tmp_path):
    repository = FilteringDiagnosticsRepository(tmp_path)
    repository.save({"date": "2026-10-05", "candidates": [{"symbol": "7203", "ratio": 0.7}]})
    repository.save({"date": "2026-10-06", "candidates": [{"symbol": "8306", "ratio": 1.2}]})

    result = repository.load_latest_for_date("2026-10-05")

    assert result["candidates"][0]["symbol"] == "7203"