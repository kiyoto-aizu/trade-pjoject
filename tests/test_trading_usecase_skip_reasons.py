import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.application import trading_usecase as trading_usecase_module
from src.application.trading_usecase import TradingUseCase
from src.config import config
from src.domain.enums import OrderSide
from src.domain.models import PriceLimit
from src.domain.volatility import VolatilityLevel
from src.infrastructure.market_data import get_daily_closes
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository

_CALCULATE_PRICE_LIMIT = trading_usecase_module.calculate_price_limit
_CALCULATE_RSI = trading_usecase_module.calculate_rsi
_REAL_PRICE_LIMIT = object()
_REAL_RSI = object()


class _OrderSender:
    def __init__(self, response=None):
        self.response = response
        self.orders = []

    def set_price(self, symbol, price):
        pass

    def place_market_order(self, token, symbol, side, quantity):
        self.orders.append((symbol, side, quantity))
        return self.response


def _build_loop(
    monkeypatch,
    tmp_path,
    *,
    closes=None,
    daily_bars=None,
    boards=None,
    positions=None,
    wallet_amount=100_000.0,
    order_sender=None,
    price_limit=PriceLimit(80.0, 99.0),
    rsi=60.0,
    assessment=None,
    close_minute=3,
):
    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'MAX_ORDER_COUNT_PER_DAY', 10)
    monkeypatch.setattr(config, 'DAILY_LOSS_LIMIT_RATIO', 0.02)
    monkeypatch.setattr(config, 'ATR_PERIOD', 14)
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'skip')
    monkeypatch.setattr(config, 'ATR_CAUTION_LOT_RATIO', 0.5)
    monkeypatch.setattr(config, 'RSI_PERIOD', 2)
    monkeypatch.setattr(config, 'RSI_MINIMUM_CLOSES', 5)
    monkeypatch.setattr(config, 'ALLOW_OVERNIGHT_HOLDING', True)
    monkeypatch.setattr(config, 'MARKET_CLOSE_HOUR', 10)
    monkeypatch.setattr(config, 'MARKET_CLOSE_MINUTE', close_minute)
    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', tmp_path / 'emergency_stop.flag')
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path / 'logs')
    if price_limit is _REAL_PRICE_LIMIT:
        monkeypatch.setattr(trading_usecase_module, 'calculate_price_limit', _CALCULATE_PRICE_LIMIT)
    else:
        monkeypatch.setattr(
            trading_usecase_module, 'calculate_price_limit', lambda values: price_limit
        )
    if rsi is _REAL_RSI:
        monkeypatch.setattr(trading_usecase_module, 'calculate_rsi', _CALCULATE_RSI)
    else:
        monkeypatch.setattr(trading_usecase_module, 'calculate_rsi', lambda *args, **kwargs: rsi)
    monkeypatch.setattr(
        trading_usecase_module,
        'assess_volatility',
        lambda *args, **kwargs: assessment,
    )

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 30 if closes is None else closes

        def get_yahoo_daily_bars(self, symbol):
            return [object()] * 14 if daily_bars is None else daily_bars

    class BoardClient:
        def __init__(self):
            self._boards = iter(boards or [{'current_price': 100.0}])
            self._last_board = boards[-1] if boards else {'current_price': 100.0}

        def get_current_board(self, token, symbol):
            return next(self._boards, self._last_board)

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': wallet_amount}

    class PositionsClient:
        def get_positions(self, token):
            return positions or []

    sender = order_sender or _OrderSender()
    repository = FilterDecisionRepository(tmp_path / 'filter_decisions.sqlite3')
    use_case = TradingUseCase(
        token='test',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        order_sender=sender,
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / 'reports',
        filter_decision_repository=repository,
        kill_switch_baseline_path=tmp_path / 'kill_switch_baseline.json',
    )
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    tick = [0]
    times = [datetime(2026, 9, 25, 10, minute) for minute in range(close_minute + 1)]

    def now_provider():
        return times[min(tick[0], len(times) - 1)]

    def sleep(_seconds):
        tick[0] += 1

    return use_case, sender, repository, symbols_path, now_provider, sleep


@pytest.mark.parametrize(
    ('response', 'reason_code'),
    [
        (None, 'ORDER_REJECTED_NONE'),
        ({'Result': 1, 'Message': 'rejected'}, 'ORDER_REJECTED_RESULT'),
    ],
)
def test_order_rejection_reason_is_logged_once(
    monkeypatch, tmp_path, caplog, response, reason_code
):
    sender = _OrderSender(response)
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch, tmp_path, order_sender=sender, close_minute=3
    )

    with caplog.at_level(logging.ERROR, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if reason_code in record.message]
    assert len(matching) == 1
    assert '7203' in matching[0].message
    assert 'BUY' in matching[0].message
    assert '300' in matching[0].message
    if response is None:
        assert '応答=None' in matching[0].message
    else:
        assert 'rejected' in matching[0].message
    assert len(sender.orders) > 1


def test_insufficient_rsi_history_is_logged_once_with_counts(monkeypatch, tmp_path, caplog):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        closes=[100.0, 101.0, 102.0],
        price_limit=_REAL_PRICE_LIMIT,
        rsi=_REAL_RSI,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'INSUFFICIENT_RSI_HISTORY' in record.message]
    assert len(matching) == 1
    assert '必要本数=5' in matching[0].message
    assert '実績本数=3' in matching[0].message


def test_rsi_unavailable_with_enough_but_invalid_history_is_logged(monkeypatch, tmp_path, caplog):
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        closes=[100.0, 101.0, 0.0, 102.0, 103.0],
        rsi=_REAL_RSI,
        price_limit=PriceLimit(99.0, 101.0),
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'RSI_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '必要本数=5' in matching[0].message
    assert '実績本数=5' in matching[0].message
    assert sender.orders == []


def test_board_unavailable_and_recovery_are_logged_once(monkeypatch, tmp_path, caplog):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[None, {'current_price': None}, {'current_price': 100.0}, None],
        price_limit=PriceLimit(99.0, 101.0),
        rsi=50.0,
        close_minute=4,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    unavailable = [record for record in caplog.records if 'BOARD_UNAVAILABLE' in record.message]
    recovered = [record for record in caplog.records if 'BOARD_RECOVERED' in record.message]
    assert len(unavailable) == 1
    assert len(recovered) == 1
    assert '7203' in unavailable[0].message
    assert '7203' in recovered[0].message


@pytest.mark.parametrize(
    ('daily_bars', 'available_bars'),
    [([], 0), ([object()] * 13, 13)],
)
def test_atr_data_unavailable_for_holding_is_logged_once(
    monkeypatch, tmp_path, caplog, daily_bars, available_bars
):
    use_case, _, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        daily_bars=daily_bars,
        positions=[{
            'Symbol': '7203',
            'Side': OrderSide.SELL.value,
            'HoldQty': 100,
            'AveragePrice': 100.0,
        }],
        price_limit=PriceLimit(80.0, 120.0),
        rsi=50.0,
        assessment=None,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'ATR_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '必要本数=14' in matching[0].message
    assert f'実績本数={available_bars}' in matching[0].message


def test_empty_yahoo_daily_response_is_warned_once(monkeypatch, caplog):
    monkeypatch.setattr(get_daily_closes, '_EMPTY_DAILY_WARNINGS', set(), raising=False)
    monkeypatch.setattr(get_daily_closes.request_handler, 'send_get', lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger='src.infrastructure.market_data.get_daily_closes'):
        assert get_daily_closes.get_yahoo_daily_closes('7203') == []
        assert get_daily_closes.get_yahoo_daily_closes('7203') == []

    matching = [record for record in caplog.records if 'DAILY_DATA_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert '7203' in matching[0].message


def test_eod_missing_price_is_warned_without_changing_attempt(monkeypatch, tmp_path, caplog):
    use_case, sender, _, _, _, _ = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[None],
        positions=[{
            'Symbol': '7203',
            'Side': OrderSide.SELL.value,
            'HoldQty': 100,
            'AveragePrice': 100.0,
        }],
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case._liquidate_all_positions()

    matching = [record for record in caplog.records if 'EOD_PRICE_UNAVAILABLE' in record.message]
    assert len(matching) == 1
    assert sender.orders == [('7203', OrderSide.SELL.value, 100)]
    assert use_case._liquidation_results[0]['status'] == '売却注文失敗'


@pytest.mark.parametrize(
    ('wallet_amount', 'assessment', 'quantity_override', 'reason_code'),
    [
        (10_000.0, None, None, 'budget_below_one_lot'),
        (
            100_000.0,
            SimpleNamespace(atr=10.0, ratio=3.0, level=VolatilityLevel.DANGER, latest_true_range=30.0),
            None,
            'atr_danger_skip',
        ),
        (
            100_000.0,
            SimpleNamespace(atr=1.0, ratio=1.7, level=VolatilityLevel.CAUTION, latest_true_range=2.0),
            50,
            'caution_rounding_to_zero',
        ),
    ],
)
def test_zero_quantity_reason_is_logged_once(
    monkeypatch, tmp_path, caplog, wallet_amount, assessment, quantity_override, reason_code
):
    if quantity_override is not None:
        monkeypatch.setattr(
            trading_usecase_module,
            'calculate_buy_quantity',
            lambda *args, **kwargs: quantity_override,
        )
    use_case, sender, repository, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        wallet_amount=wallet_amount,
        assessment=assessment,
        price_limit=PriceLimit(80.0, 99.0),
        rsi=60.0,
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if reason_code in record.message]
    assert len(matching) == 1
    assert '7203' in matching[0].message
    assert sender.orders == []
    if reason_code == 'atr_danger_skip':
        events = repository.load_summaries()
        assert any(event['event_type'] == 'ATR_DANGER_SKIP' for event in events)


def test_zero_price_quantity_input_is_logged(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        trading_usecase_module.TradeSignal,
        'evaluate',
        classmethod(lambda cls, symbol, current_price, *args, **kwargs: SimpleNamespace(
            symbol=symbol, side=OrderSide.BUY, price=0.0, qty=0
        )),
    )
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        boards=[{'current_price': 0.0}],
    )

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [record for record in caplog.records if 'buy_quantity_invalid_inputs' in record.message]
    assert len(matching) == 1
    assert sender.orders == []


def test_danger_minimum_adjustment_to_zero_has_reason(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'minimum')
    monkeypatch.setattr(
        trading_usecase_module,
        'calculate_buy_quantity',
        lambda *args, **kwargs: 50,
    )
    assessment = SimpleNamespace(
        atr=10.0,
        ratio=3.0,
        level=VolatilityLevel.DANGER,
        latest_true_range=30.0,
    )
    use_case, sender, _, symbols_path, now_provider, sleep = _build_loop(
        monkeypatch,
        tmp_path,
        assessment=assessment,
    )
    monkeypatch.setattr(config, 'ATR_DANGER_ACTION', 'minimum')

    with caplog.at_level(logging.WARNING, logger='src.application.trading_usecase'):
        use_case.run(symbols_path, now_provider=now_provider, sleep=sleep)

    matching = [
        record for record in caplog.records
        if 'atr_quantity_adjustment_zero' in record.message
    ]
    assert len(matching) == 1
    assert sender.orders == []
