import logging
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src.application import trading_usecase as trading_usecase_module
from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.domain.enums import OrderSide
from src.domain.models import PriceLimit
from src.infrastructure.paper.paper_order_client import PaperOrderClient
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository


def _run_eod_scenario(
    monkeypatch,
    tmp_path,
    *,
    resume_time: datetime,
    quote: dict | None,
    allow_overnight: bool = False,
):
    trade_date = date(2026, 9, 25)
    tick_times = [datetime(2026, 9, 25, 15, 19), resume_time]
    tick_index = [0]
    notifications = []

    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', allow_overnight)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', 30)
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_price_limit',
        lambda closes: PriceLimit(80.0, 99.0),
    )
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_rsi',
        lambda *args, **kwargs: 60.0,
    )
    monkeypatch.setattr(trading_usecase_module, 'assess_volatility', lambda *args, **kwargs: None)

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 30

        def get_yahoo_daily_bars(self, symbol):
            return []

    class BoardClient:
        freshness_requests = 0

        def get_current_board(self, token, symbol):
            return {'current_price': 100.0}

        def get_current_board_with_freshness(self, token, symbol):
            self.freshness_requests += 1
            return quote

    board_client = BoardClient()
    order_sender = PaperOrderClient(
        prices={},
        cash=100_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=None,
        realized_pnl_date=trade_date.isoformat(),
        today_provider=lambda: trade_date,
    )
    use_case = TradingUseCase(
        token='test',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=board_client,
        order_sender=order_sender,
        notifier=notifications.append,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / 'reports',
        filter_decision_repository=FilterDecisionRepository(
            tmp_path / 'filter_decisions.sqlite3'
        ),
        kill_switch_baseline_path=tmp_path / 'kill_switch_baseline.json',
    )
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(' ["7203"] ', encoding='utf-8')

    def now_provider():
        return tick_times[min(tick_index[0], len(tick_times) - 1)]

    def sleep(_seconds):
        tick_index[0] += 1

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=now_provider,
        sleep=sleep,
    )
    return use_case, order_sender, board_client, notifications


def _quote(
    *,
    price: float = 100.0,
    quote_time: str = '2026-09-25T15:14:00+09:00',
    status: int = 1,
) -> dict:
    return {
        'current_price': price,
        'current_price_time': quote_time,
        'current_price_status': status,
    }


def test_normal_1520_liquidation_accepts_same_day_trade_from_six_minutes_earlier(
    monkeypatch, tmp_path
):
    use_case, order_sender, board_client, _ = _run_eod_scenario(
        monkeypatch,
        tmp_path,
        resume_time=datetime(2026, 9, 25, 15, 20),
        quote=_quote(),
    )

    assert [order['Side'] for order in order_sender.orders] == [
        OrderSide.BUY.value,
        OrderSide.SELL.value,
    ]
    assert order_sender.get_positions('test') == []
    assert use_case.order_history[-1].decision_reason == '持ち越し防止'
    assert board_client.freshness_requests == 1


@pytest.mark.parametrize(
    ('quote', 'reason'),
    [
        (None, 'LIQUIDATION_BOARD_UNAVAILABLE'),
        (_quote(quote_time='2026-09-24T15:29:00+09:00'), 'LIQUIDATION_PRICE_NOT_TODAY'),
        (_quote(status=2), 'LIQUIDATION_PRICE_STATUS_INVALID'),
        (_quote(price=0.0), 'LIQUIDATION_PRICE_INVALID'),
        (_quote(price=float('inf')), 'LIQUIDATION_PRICE_INVALID'),
        (_quote(quote_time='2026-09-25T15:19:00'), 'LIQUIDATION_PRICE_TIME_INVALID'),
    ],
)
def test_invalid_late_quote_leaves_position_and_aggregates_one_alert(
    monkeypatch, tmp_path, caplog, quote, reason
):
    use_case, order_sender, board_client, notifications = _run_eod_scenario(
        monkeypatch,
        tmp_path,
        resume_time=datetime(2026, 9, 25, 15, 31),
        quote=quote,
    )

    assert [order['Side'] for order in order_sender.orders] == [OrderSide.BUY.value]
    assert order_sender.get_positions('test')[0]['HoldQty'] == 300
    assert use_case._liquidation_results[0]['reason'] == reason
    emergency_messages = [
        message for message in notifications
        if message.startswith('【緊急】EOD決済未完了')
    ]
    assert len(emergency_messages) == 1
    assert '7203' in emergency_messages[0]
    assert reason in emergency_messages[0]
    assert any('EOD_LIQUIDATION_UNRESOLVED' in record.message for record in caplog.records)
    assert board_client.freshness_requests == 1


def test_overnight_enabled_does_not_attempt_late_liquidation(monkeypatch, tmp_path):
    _, order_sender, board_client, _ = _run_eod_scenario(
        monkeypatch,
        tmp_path,
        resume_time=datetime(2026, 9, 25, 15, 31),
        quote=_quote(status=8, quote_time='2026-09-25T15:30:00+09:00'),
        allow_overnight=True,
    )

    assert [order['Side'] for order in order_sender.orders] == [OrderSide.BUY.value]
    assert order_sender.get_positions('test')[0]['HoldQty'] == 300
    assert board_client.freshness_requests == 0


def test_startup_warns_about_restored_position_without_selling(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', False)
    state_path = tmp_path / 'paper_state.json'
    seed = PaperOrderClient(
        prices={'7203': 100.0},
        cash=100_000.0,
        fee_rate=0.0,
        market_slippage_bps=0.0,
        state_path=state_path,
    )
    seed.place_market_order('test', '7203', OrderSide.BUY.value, 100)
    restored = PaperOrderClient(prices={'7203': 100.0}, state_path=state_path)
    use_case = TradingUseCase(
        token='test',
        order_history_path=tmp_path / 'order_history.json',
        order_sender=restored,
        filter_decision_repository=FilterDecisionRepository(
            tmp_path / 'filter_decisions.sqlite3'
        ),
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.warn_on_overnight_positions()

    warnings = [
        record for record in caplog.records
        if 'OVERNIGHT_POSITION_AT_START' in record.message
    ]
    assert len(warnings) == 1
    assert '7203' in warnings[0].message
    assert '100' in warnings[0].message
    assert restored.get_positions('test')[0]['HoldQty'] == 100
    assert restored.orders == []