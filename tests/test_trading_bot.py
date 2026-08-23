import json
from pathlib import Path
from datetime import datetime, timedelta

import pytest

from src.config import config
from src.trading.trading import TradingBot
from src.infrastructure.persistence.order_history import OrderHistoryManager
from src.infrastructure.kabu.account import AccountManager


class DummyWalletApi:
    def __init__(self, wallet_data):
        self.wallet_data = wallet_data

    def get_wallet_cash(self, token):
        return self.wallet_data


class DummyPositionsApi:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self, token):
        return self._positions


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv('API_PASSWORD_DEV', 'dummy')
    monkeypatch.setenv('IS_DEMO', 'true')
    return monkeypatch


def test_required_env_helper_raises_when_missing():
    with pytest.raises(ValueError, match='Missing required environment variable: MISSING_ENV_FOR_TEST'):
        from src.config import config
        config._load_required_env('MISSING_ENV_FOR_TEST', allow_missing=False)


def test_order_history_load_corrupt(tmp_path):
    history_file = tmp_path / 'order_history.json'
    history_file.write_text('not-json', encoding='utf-8')

    with pytest.raises(ValueError):
        OrderHistoryManager(history_file)


def test_order_history_register_and_query(tmp_path):
    history_file = tmp_path / 'order_history.json'
    manager = OrderHistoryManager(history_file)

    manager.register_order('1475', config.OrderSide.BUY.value, 1000.0, 100)
    assert manager.has_ordered_today('1475', config.OrderSide.BUY.value)
    assert not manager.has_ordered_today('1475', config.OrderSide.SELL.value)
    assert len(manager.todays_orders()) == 1


def test_account_manager_has_holdings():
    account = AccountManager(token='dummy')
    account.positions = [
        {'Symbol': '1475', 'Side': config.OrderSide.SELL.value, 'HoldQty': '100'},
        {'Symbol': '7203', 'Side': '1', 'HoldQty': '0'},
    ]

    assert account.has_holdings('1475')
    assert not account.has_holdings('7203')
    assert not account.has_holdings('9999')
