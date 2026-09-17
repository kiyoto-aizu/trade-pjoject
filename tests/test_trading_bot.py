import json
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import config
from src.domain.models import PriceLimit, TradeSignal
from src.domain.market_regime import MarketRegime
from src.infrastructure.persistence.filter_decision_repository import FilterDecisionRepository
from src.domain.volatility import VolatilityLevel
from src.domain.rules import is_market_closed
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.paper.paper_order_executor import PaperOrderExecutor
from src.entrypoints.run_trading import create_trading_use_case


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv('API_PASSWORD_DEV', 'dummy')
    monkeypatch.setenv('IS_DEMO', 'true')
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "RSI_ENTRY_THRESHOLD", 50.0)
    monkeypatch.setattr(config, "RSI_EXIT_THRESHOLD", 50.0)
    return monkeypatch


def test_required_env_helper_raises_when_missing():
    with pytest.raises(ValueError, match='Missing required environment variable: MISSING_ENV_FOR_TEST'):
        from src.config import config
        config._load_required_env('MISSING_ENV_FOR_TEST', allow_missing=False)


def test_application_logs_are_stored_under_data_logs():
    assert Path(config.LOG_FILE_PATH) == config.LOG_DIRECTORY / 'pytest.log'
    assert config.LOG_DIRECTORY.name == 'logs'
    assert config.LOG_DIRECTORY.parent.name == 'data'


def test_is_market_closed_boundary():
    assert not is_market_closed(time(15, 29), 15, 30)
    assert is_market_closed(time(15, 30), 15, 30)
    assert is_market_closed(time(16, 0), 15, 30)


def test_trade_signal_follows_trend_and_exits_when_it_weakens():
    limit = PriceLimit(lower_band=99.0, upper_band=101.0)

    entry = TradeSignal.evaluate('7203', 102.0, limit, 60.0, 55.0, 45.0)
    exit_signal = TradeSignal.evaluate('7203', 98.0, limit, 40.0, 55.0, 45.0)

    assert entry is not None
    assert entry.side == config.OrderSide.BUY
    assert exit_signal is not None
    assert exit_signal.side == config.OrderSide.SELL


def test_order_history_load_corrupt(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text('not-json', encoding='utf-8')

    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    with pytest.raises(ValueError):
        use_case._load_order_history()


def test_order_history_register_records_audit_fields(tmp_path):
    history_file = tmp_path / 'order_history.json'
    use_case = TradingUseCase(token='dummy', order_history_path=history_file)
    use_case._load_order_history()

    signal = TradeSignal(symbol='1475', side=config.OrderSide.BUY, price=1000.0, qty=100)
    limit = PriceLimit(lower_band=990.0, upper_band=1010.0)
    messages = []
    use_case.notifier = messages.append
    use_case._register_order(signal, limit, {'Result': 0, 'OrderId': 'abc123'})

    assert len(use_case.order_history) == 1
    entry = use_case.order_history[0]
    assert entry.symbol == '1475'
    assert entry.result_code == 0
    assert entry.order_id == 'abc123'
    assert entry.basis_lower_band == 990.0
    assert entry.basis_upper_band == 1010.0
    assert messages[0].startswith('【業務】取引運用\n【機能】注文執行')
    assert '買い注文が成立しました。' in messages[0]
    assert '銘柄: 1475' in messages[0]

    # 再読み込みしても永続化されていること
    reloaded = TradingUseCase(token='dummy', order_history_path=history_file)
    reloaded._load_order_history()
    assert len(reloaded.order_history) == 1
    assert reloaded.order_history[0].order_id == 'abc123'


def test_has_holdings_checks_sell_side_positions():
    use_case = TradingUseCase(token='dummy', order_history_path=Path('unused_history.json'))
    positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': '1', 'HoldQty': '0'},
    ]

    assert use_case._has_holdings('1475', positions)
    assert not use_case._has_holdings('7203', positions)
    assert not use_case._has_holdings('9999', positions)


def test_count_open_positions_counts_only_open_sell_side_positions():
    use_case = TradingUseCase(token='dummy', order_history_path=Path('unused_history.json'))
    positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': config.OrderSide.SELL.value, 'HoldQty': '0'},
        {'Symbol': '8306', 'Side': config.OrderSide.BUY.value, 'HoldQty': '100'},
    ]

    assert use_case._count_open_positions(positions) == 1


def test_trading_use_case_sizes_new_buy_from_current_wallet(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 30_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    clock_calls = 0

    def now_provider():
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2026, 9, 4, 10, 0) if clock_calls <= 2 else datetime(2026, 9, 4, 15, 30)

    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=now_provider, sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 300)]


def test_trading_use_case_sizes_new_buy_by_remaining_position_slots(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [2_000.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 2_200.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('1301', '1332', '1605')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 1_000_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 1_000_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    # Fixed five-slot allocation would allow only 100,000 yen, below one 220,000-yen lot.
    assert orders == [('7203', config.OrderSide.BUY.value, 100)]


def test_trading_use_case_uses_one_remaining_slot_for_existing_holding_at_position_limit(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [2_000.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 2_200.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('7203', '1301', '1332', '1605', '1801')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 1_000_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 1_000_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 200)]


def test_trading_use_case_keeps_buy_budget_caps_with_remaining_position_slots(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    orders = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [900.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 1_000.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 500_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': symbol, 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for symbol in ('1301', '1332', '1605')
            ]

    class OrderSender:
        def place_market_order(self, token, symbol, side, qty):
            orders.append((symbol, side, qty))
            return {'Result': 0, 'OrderId': 'order-1'}

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 5)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 300_000.0)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 150_000.0)
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [('7203', config.OrderSide.BUY.value, 100)]


def test_trading_use_case_monitors_all_candidates_when_position_limit_is_reached(monkeypatch, tmp_path, caplog):
    symbols = [str(1000 + index) for index in range(10)]
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(symbols), encoding='utf-8')
    observed_symbols = []
    caplog.set_level('INFO', logger='src.application.trading_usecase')

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            observed_symbols.append(symbol)
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return [
                {'Symbol': str(index), 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'}
                for index in range(3)
            ]

    class OrderSender:
        def place_market_order(self, *args):
            raise AssertionError('保有上限時の新規注文は実行されない')

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'TARGET_POSITIONS', 3)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    clock_calls = 0

    def now_provider():
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2026, 9, 4, 10, 0) if clock_calls == 1 else datetime(2026, 9, 4, 15, 30)

    use_case = TradingUseCase(
        token='dummy', order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(), board_client=BoardClient(),
        wallet_client=WalletClient(), positions_client=PositionsClient(),
        order_sender=OrderSender(), notifier=lambda message: None,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=now_provider, sleep=lambda seconds: None)

    assert observed_symbols == symbols
    assert '保有上限のため新規買いを見送ります' in caplog.text


def test_trading_use_case_factory_uses_paper_executor_by_default(monkeypatch):
    monkeypatch.setattr(config, 'TRADING_MODE', 'paper')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ENABLE_LIVE_ORDERING', False)

    bot = create_trading_use_case(token='dummy')

    assert isinstance(bot.order_sender, PaperOrderExecutor)


def test_trading_use_case_factory_rejects_live_mode_without_explicit_production_settings(monkeypatch):
    monkeypatch.setattr(config, 'TRADING_MODE', 'live')
    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'ENABLE_LIVE_ORDERING', True)

    with pytest.raises(ValueError, match='ライブ注文には'):
        create_trading_use_case(token='dummy')


def test_end_of_day_report_identifies_paper_trading(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'TRADING_MODE_LABEL', 'ペーパートレード')
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case._send_end_of_day_report()

    assert messages[0].startswith('【業務】取引運用\n【機能】取引終了')
    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['order_count'] == 0
    assert report['report_text'] == messages[0]


def test_end_of_day_report_keeps_market_conditions_in_data_but_not_notification(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.market_regime = MarketRegime.CAUTION
    use_case.market_regime_assessment = SimpleNamespace(
        realized_volatility_percent=21.5,
        vix=19.2,
        nikkei_change_percent=-1.1,
        adx=24.0,
        data_available=True,
        failure_reason=None,
    )
    use_case._register_order(
        TradeSignal('7203', config.OrderSide.BUY, 100.0, 100),
        PriceLimit(95.0, 105.0),
        {'Result': 0, 'OrderId': 'order-1'},
        SimpleNamespace(
            atr=3.2,
            latest_true_range=4.8,
            ratio=1.5,
            level=VolatilityLevel.CAUTION,
        ),
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions'] == {
        'assessment_status': 'available',
        'regime': 'CAUTION',
        'realized_volatility_percent': 21.5,
        'vix': 19.2,
        'nikkei_change_percent': -1.1,
        'adx': 24.0,
        'data_available': True,
        'failure_reason': None,
    }
    assert report['orders'][0]['atr'] == 3.2
    assert report['orders'][0]['atr_level'] == 'CAUTION'
    assert 'MarketRegime: CAUTION' not in messages[-1]
    assert 'ATR: 3.200円' in messages[-1]


def test_end_of_day_report_marks_market_conditions_not_evaluated(tmp_path):
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions'] == {
        'assessment_status': 'not_evaluated',
        'regime': None,
        'realized_volatility_percent': None,
        'vix': None,
        'nikkei_change_percent': None,
        'adx': None,
        'data_available': None,
        'failure_reason': None,
    }


def test_end_of_day_report_marks_market_conditions_unavailable(tmp_path):
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=lambda message: None,
        daily_analyzer=SimpleNamespace(analyze=lambda daily_summary: None),
        daily_report_directory=tmp_path / 'reports',
    )
    use_case.market_regime = MarketRegime.DANGER
    use_case.market_regime_assessment = SimpleNamespace(
        realized_volatility_percent=None,
        vix=None,
        nikkei_change_percent=None,
        adx=None,
        data_available=False,
        failure_reason='日経225またはVIXの日足データが空です',
    )

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['market_conditions']['assessment_status'] == 'unavailable'
    assert report['market_conditions']['regime'] == 'DANGER'
    assert report['market_conditions']['data_available'] is False
    assert report['market_conditions']['failure_reason'] == '日経225またはVIXの日足データが空です'


def test_end_of_day_report_counts_only_errors_during_trading_session(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'LOG_DIRECTORY', tmp_path)
    today = datetime.now().date().isoformat()
    (tmp_path / 'trade_project.log').write_text(
        f"{today} 08:59:59 ERROR test: before market\n"
        f"{today} 09:00:00 ERROR test: opening error\n"
        f"{today} 15:29:59 ERROR test: closing error\n"
        f"{today} 15:30:00 ERROR test: after market\n",
        encoding='utf-8',
    )
    use_case = TradingUseCase(token='dummy', order_history_path=tmp_path / 'order_history.json')

    summary = use_case._daily_log_error_summary(today)

    assert summary['count'] == 2
    assert summary['summaries'] == ['opening error', 'closing error']


def test_end_of_day_report_includes_atr_danger_skip_outcome(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    assessment = SimpleNamespace(atr=5.0, latest_true_range=12.0, ratio=2.4)
    use_case._record_atr_danger_skip('3624', datetime(2026, 9, 15, 14, 55), 95.0, assessment, 300)
    use_case._update_atr_danger_skip_observation('3624', datetime(2026, 9, 15, 15, 19), 97.0)

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    skip = report['atr_danger_skips'][0]
    assert skip['hypothetical_pnl_before_cost'] == 600.0
    assert skip['outcome'] == '利益取り逃しの可能性'
    assert '3624: 利益取り逃しの可能性 (+600円概算)' in messages[0]


def test_end_of_day_report_includes_atr_stop_exit_outcome(tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )
    assessment = SimpleNamespace(atr=5.0, ratio=2.4, level=VolatilityLevel.DANGER)
    use_case._record_atr_stop_exit(
        '3624', datetime(2026, 9, 15, 14, 55), 100.0, 96.5, 300, assessment, 0.7
    )
    use_case._update_atr_stop_exit_observation('3624', datetime(2026, 9, 15, 15, 19), 94.5)

    use_case._send_end_of_day_report()

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    exit_summary = report['atr_stop_exits'][0]
    assert exit_summary['avoided_pnl_before_cost'] == 600.0
    assert exit_summary['outcome'] == '下落回避の可能性'
    assert '3624: 下落回避の可能性 (+600円概算)' in messages[0]


def test_end_of_day_report_appends_daily_llm_analysis(tmp_path):
    messages = []
    summaries = []

    class DailyAnalyzer:
        def analyze(self, summary):
            summaries.append(summary)
            return '今日の評価\n- 参考評価です。'

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
        daily_analyzer=DailyAnalyzer(),
        daily_report_directory=tmp_path / 'reports',
    )
    use_case._send_end_of_day_report()

    assert summaries[0]['order_count'] == 0
    assert summaries[0]['positions'] == []
    assert 'LLM日次評価（参考）' in messages[0]
    assert '参考評価です。' in messages[0]
    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['llm_analysis'] == '今日の評価\n- 参考評価です。'


@pytest.mark.parametrize(
    ("regime", "expected_order_count"),
    [
        (MarketRegime.NORMAL, 1),
        (MarketRegime.CAUTION, 0),
    ],
)
def test_trading_use_case_applies_market_regime_to_entry_threshold(
    monkeypatch,
    tmp_path,
    regime,
    expected_order_count,
):
    monkeypatch.setattr(config, 'IS_DEMO', True)
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    calls = []

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class NoOpAnalyzer:
        def analyze(self, summary):
            return None

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            calls.append((token, symbol, side))
            return {'Result': 0, 'OrderId': 'paper-order-1'}

    class MarketRegimeProvider:
        def __init__(self):
            self.calls = 0

        def execute(self):
            self.calls += 1
            return SimpleNamespace(
                regime=regime,
                data_available=True,
                failure_reason=None,
            )

    market_regime_provider = MarketRegimeProvider()
    filter_decision_repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")

    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.get_api_soft_limit',
        lambda token: (_ for _ in ()).throw(AssertionError('live soft-limit API was called')),
    )

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        order_sender=OrderSender(),
        notifier=lambda message: None,
        market_regime_usecase=market_regime_provider,
        filter_decision_repository=filter_decision_repository,
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert len(calls) == expected_order_count
    assert len(use_case.order_history) == expected_order_count
    if expected_order_count:
        assert calls == [('dummy', '7203', config.OrderSide.BUY.value)]
        assert use_case.order_history[0].result_code == 0
        assert use_case.order_history[0].order_id == 'paper-order-1'
    event_types = [
        item["event_type"]
        for item in filter_decision_repository.load_summaries(execution_mode="paper")
    ]
    if regime == MarketRegime.CAUTION:
        assert event_types == ["MARKET_REGIME_CAUTION_RSI_FILTER"]
    else:
        assert event_types == []
    assert market_regime_provider.calls == 1


def test_trading_use_case_records_adx_relief_after_successful_buy(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IS_DEMO", True)
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    filter_decision_repository = FilterDecisionRepository(tmp_path / "filter_decisions.sqlite3")

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            return {"Result": 0, "OrderId": "paper-order-1"}

    class MarketRegimeProvider:
        def execute(self):
            return SimpleNamespace(
                regime=MarketRegime.NORMAL, data_available=True, failure_reason=None,
                trend_relief_applied=True, adx=30.0,
            )

    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 100_000.0)
    use_case = TradingUseCase(
        token="dummy", order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(), board_client=BoardClient(), wallet_client=WalletClient(),
        positions_client=PositionsClient(), order_sender=OrderSender(), notifier=lambda message: None,
        market_regime_usecase=MarketRegimeProvider(), filter_decision_repository=filter_decision_repository,
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    events = filter_decision_repository.load_summaries(execution_mode="paper")
    assert [(item["event_type"], item["symbol"], item["adx"]) for item in events] == [
        ("ADX_TREND_RELIEF", "7203", 30.0)
    ]


def test_trading_use_case_continues_when_filter_decision_storage_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "IS_DEMO", True)
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    orders = []

    class FailingRepository:
        def load_open_events(self, execution_mode):
            raise OSError("storage unavailable")

        def finalize_due_events(self, as_of, observation_days, execution_mode=None):
            raise OSError("storage unavailable")

        def update_open_event_observations(self, observed_at, prices_by_symbol, execution_mode):
            raise OSError("storage unavailable")

        def record_event(self, *args, **kwargs):
            raise OSError("storage unavailable")

        def load_summaries(self, **kwargs):
            raise OSError("storage unavailable")

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class OrderSender:
        def place_market_order(self, token, symbol, side):
            orders.append((symbol, side))
            return {"Result": 0, "OrderId": "paper-order-1"}

    current_times = iter([datetime(2026, 9, 16, 10, 0), datetime(2026, 9, 16, 15, 30)])
    monkeypatch.setattr(config, "API_SOFT_LIMIT", 100_000.0)
    use_case = TradingUseCase(
        token="dummy", order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(), board_client=BoardClient(), wallet_client=WalletClient(),
        positions_client=PositionsClient(), order_sender=OrderSender(), notifier=lambda message: None,
        filter_decision_repository=FailingRepository(),
    )

    use_case.run(top_symbols_path=symbols_path, now_provider=lambda: next(current_times), sleep=lambda seconds: None)

    assert orders == [("7203", config.OrderSide.BUY.value)]


def test_trading_use_case_collects_complete_preflight_market_data(tmp_path):
    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * config.RSI_MINIMUM_CLOSES

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 101.0}

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
    )

    assert use_case.collect_preflight_market_data(['7203', '8306']) == {
        '7203': {'closes': [100.0] * config.RSI_MINIMUM_CLOSES, 'board': {'current_price': 101.0}},
        '8306': {'closes': [100.0] * config.RSI_MINIMUM_CLOSES, 'board': {'current_price': 101.0}},
    }


def test_trading_use_case_rejects_incomplete_preflight_market_data(tmp_path):
    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * (config.RSI_MINIMUM_CLOSES - 1)

    class BoardClient:
        def get_current_board(self, token, symbol):
            raise AssertionError('board should not be called')

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
    )

    assert use_case.collect_preflight_market_data(['7203']) is None


def test_trading_use_case_does_not_record_rejected_order(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    history_path = tmp_path / 'order_history.json'
    order_calls = []
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 15, 30)])

    monkeypatch.setattr('src.application.trading_usecase.get_yahoo_daily_closes', lambda symbol: [90.0] * 5)
    monkeypatch.setattr(
        'src.application.trading_usecase.get_current_board',
        lambda token, symbol: {'current_price': 92.0},
    )
    monkeypatch.setattr(
        'src.application.trading_usecase.get_wallet_cash',
        lambda token: {'StockAccountWallet': 100_000.0},
    )
    monkeypatch.setattr('src.application.trading_usecase.get_positions', lambda token: [])
    monkeypatch.setattr('src.application.trading_usecase.get_api_soft_limit', lambda token: 100_000.0)
    monkeypatch.setattr(
        'src.application.trading_usecase.place_market_order',
        lambda token, symbol, side: order_calls.append((symbol, side)) or {'Result': 1, 'OrderId': 'rejected'},
    )

    use_case = TradingUseCase(
        token='dummy',
        order_history_path=history_path,
        notifier=lambda message: None,
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert order_calls == [('7203', config.OrderSide.BUY.value)]
    assert use_case.order_history == []


def test_trading_use_case_liquidates_all_holdings_before_market_close(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    executor = PaperOrderExecutor(prices={'7203': 100.0}, cash=20_000.0, order_qty=100)
    executor.place_market_order('unused', '7203', config.OrderSide.BUY.value)

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 101.0}

    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_HOUR', 15)
    monkeypatch.setattr(config, 'MARKET_LIQUIDATION_MINUTE', 20)
    use_case = TradingUseCase(
        token='unused',
        order_history_path=tmp_path / 'order_history.json',
        board_client=BoardClient(),
        order_sender=executor,
        notifier=lambda message: None,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: datetime(2026, 9, 4, 15, 20),
        sleep=lambda seconds: None,
    )

    assert executor.holdings == {}
    assert executor.orders[-1]['Side'] == config.OrderSide.SELL.value
    assert executor.orders[-1]['Qty'] == 100
    assert use_case.order_history[-1].side == config.OrderSide.SELL
    assert use_case.order_history[-1].qty == 100


def test_trading_use_case_records_kill_switch_in_daily_report(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    messages = []
    current_times = iter([datetime(2026, 9, 4, 10, 0), datetime(2026, 9, 4, 10, 0)])

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    class NoOpAnalyzer:
        def analyze(self, summary):
            return None

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr('src.application.trading_usecase.check_kill_switch', lambda *args: False)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        notifier=messages.append,
        daily_analyzer=NoOpAnalyzer(),
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert use_case.kill_switch_triggered is True
    assert report['kill_switch_triggered'] is True
    assert '【緊急停止】キルスイッチを発動しました' in messages[0]
    assert any('キルスイッチ: 発動' in message for message in messages)


def test_kill_switch_notifies_only_once(monkeypatch, tmp_path):
    messages = []
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        notifier=messages.append,
    )

    use_case._trigger_kill_switch('損失上限超過')
    use_case._trigger_kill_switch('重複通知')

    assert messages == ['【緊急停止】キルスイッチを発動しました: 損失上限超過']


def test_emergency_stop_liquidates_positions_and_stops_loop(monkeypatch, tmp_path):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    stop_file = tmp_path / 'emergency_stop'
    stop_file.write_text('requested', encoding='utf-8')
    messages = []
    orders = []

    class PositionsClient:
        def get_positions(self, token):
            return [{'Symbol': '7203', 'Side': config.OrderSide.SELL.value, 'HoldQty': 100}]

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 92.0}

    class OrderSender:
        def set_price(self, symbol, price):
            pass

        def place_market_order(self, token, symbol, side, quantity):
            orders.append((symbol, side, quantity))
            return {'Result': 0, 'OrderId': 'emergency-1'}

    monkeypatch.setattr(config, 'EMERGENCY_STOP_FILE', stop_file)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        positions_client=PositionsClient(),
        wallet_client=WalletClient(),
        board_client=BoardClient(),
        order_sender=OrderSender(),
        notifier=messages.append,
        daily_report_directory=tmp_path / 'reports',
    )

    use_case.run(top_symbols_path=symbols_path, sleep=lambda seconds: None)

    assert use_case.emergency_stop_triggered is True
    assert orders == [('7203', config.OrderSide.SELL.value, 100)]
    assert any('手動緊急停止フラグ' in message for message in messages)


def test_trading_use_case_warns_once_for_repeated_sell_signal_without_holdings(monkeypatch, tmp_path, caplog):
    symbols_path = tmp_path / 'top_symbols.json'
    symbols_path.write_text(json.dumps(['7203']), encoding='utf-8')
    current_times = iter([
        datetime(2026, 9, 4, 10, 0),
        datetime(2026, 9, 4, 10, 0),
        datetime(2026, 9, 4, 10, 1),
        datetime(2026, 9, 4, 10, 1),
        datetime(2026, 9, 4, 15, 30),
    ])

    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [90.0] * 5

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {'current_price': 89.0}

    class WalletClient:
        def get_wallet_cash(self, token):
            return {'StockAccountWallet': 100_000.0}

    class PositionsClient:
        def get_positions(self, token):
            return []

    monkeypatch.setattr(config, 'IS_DEMO', True)
    monkeypatch.setattr(config, 'API_SOFT_LIMIT', 100_000.0)
    monkeypatch.setattr(config, 'MAX_ORDER_AMOUNT_PER_TRADE', 100_000.0)
    use_case = TradingUseCase(
        token='dummy',
        order_history_path=tmp_path / 'order_history.json',
        market_data_client=MarketDataClient(),
        board_client=BoardClient(),
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        notifier=lambda message: None,
    )

    with caplog.at_level('WARNING', logger='src.domain.rules'):
        use_case.run(
            top_symbols_path=symbols_path,
            now_provider=lambda: next(current_times),
            sleep=lambda seconds: None,
        )

    warnings = [
        record for record in caplog.records
        if record.message == '保有株が確認できないため、売り注文を見送ります。'
    ]
    assert len(warnings) == 1
