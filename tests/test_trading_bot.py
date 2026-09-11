import json
from datetime import datetime, time
from pathlib import Path

import pytest

from src.config import config
from src.domain.models import PriceLimit, TradeSignal
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
    assert Path(config.LOG_FILE_PATH) == config.LOG_DIRECTORY / 'trade_project.log'
    assert config.LOG_DIRECTORY.name == 'logs'
    assert config.LOG_DIRECTORY.parent.name == 'data'


def test_is_market_closed_boundary():
    assert not is_market_closed(time(15, 29), 15, 30)
    assert is_market_closed(time(15, 30), 15, 30)
    assert is_market_closed(time(16, 0), 15, 30)


def test_trade_signal_follows_trend_and_exits_when_it_weakens():
    limit = PriceLimit(buy=99.0, sell=101.0)

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
    limit = PriceLimit(buy=990.0, sell=1010.0)
    use_case._register_order(signal, limit, {'Result': 0, 'OrderId': 'abc123'})

    assert len(use_case.order_history) == 1
    entry = use_case.order_history[0]
    assert entry.symbol == '1475'
    assert entry.result_code == 0
    assert entry.order_id == 'abc123'
    assert entry.basis_buy_limit == 990.0
    assert entry.basis_sell_limit == 1010.0

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
        daily_report_directory=tmp_path / 'reports',
    )

    use_case._send_end_of_day_report()

    assert messages[0].startswith('【取引】結果（ペーパートレード）')
    report = json.loads((tmp_path / 'reports' / f'{datetime.now().date().isoformat()}.json').read_text(encoding='utf-8'))
    assert report['order_count'] == 0
    assert report['report_text'] == messages[0]


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


def test_trading_use_case_places_and_records_buy_order_without_live_api(monkeypatch, tmp_path):
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
    )
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: next(current_times),
        sleep=lambda seconds: None,
    )

    assert calls == [('dummy', '7203', config.OrderSide.BUY.value)]
    assert len(use_case.order_history) == 1
    assert use_case.order_history[0].result_code == 0
    assert use_case.order_history[0].order_id == 'paper-order-1'


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
    assert 'キルスイッチ: 発動' in messages[0]


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
