"""
強制決済(緊急停止・EOD)時に保有株一覧の取得が失敗した場合の挙動のテスト。
「取得失敗(None)」と「保有ゼロ([])」を取り違えないことを検証する。
実際のAPI・本番の状態ファイル・DB・ログには触れない。
"""
import json
from types import SimpleNamespace

import pytest

from src.config import config
from src.application import trading_usecase as trading_usecase_module
from src.application.trading_usecase import TradingUseCase


def _use_case(tmp_path, notifier, positions_client, board_client=None, order_sender=None):
    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000.0}

    return TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        positions_client=positions_client,
        wallet_client=WalletClient(),
        board_client=board_client,
        order_sender=order_sender,
        notifier=notifier,
        daily_report_directory=tmp_path / "reports",
    )


def test_emergency_stop_notifies_when_positions_fetch_fails_every_time(monkeypatch, tmp_path):
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    stop_file = tmp_path / "emergency_stop"
    stop_file.write_text("requested", encoding="utf-8")
    monkeypatch.setattr(config, "EMERGENCY_STOP_FILE", stop_file)
    monkeypatch.setattr(config, "LIQUIDATION_POSITIONS_FETCH_RETRIES", 3)
    monkeypatch.setattr(config, "LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS", 0)
    sleep_calls = []
    monkeypatch.setattr(trading_usecase_module.time, "sleep", sleep_calls.append)

    calls = []

    class PositionsClient:
        def get_positions(self, token):
            calls.append(1)
            return None  # 取得失敗が続く

    orders = []

    class OrderSender:
        def place_market_order(self, token, symbol, side, quantity):
            orders.append((symbol, side, quantity))
            return {"Result": 0, "OrderId": "should-not-happen"}

    messages = []
    use_case = _use_case(tmp_path, messages.append, PositionsClient(), order_sender=OrderSender())

    use_case.run(top_symbols_path=symbols_path, sleep=lambda seconds: None)

    assert use_case.emergency_stop_triggered is True
    assert orders == []  # 保有不明のため売却は行われない
    # 内訳: run()開始時の口座状態取得で1回 + 清算時のリトライでRETRIES回 + ループ終了後の最終リフレッシュ1回
    assert len(calls) == config.LIQUIDATION_POSITIONS_FETCH_RETRIES + 2
    assert len(sleep_calls) == config.LIQUIDATION_POSITIONS_FETCH_RETRIES - 1
    failure_messages = [m for m in messages if "強制決済" in m]
    assert len(failure_messages) == 1
    assert "取得に失敗" in failure_messages[0]


def test_emergency_stop_retries_and_succeeds_after_transient_positions_failure(monkeypatch, tmp_path):
    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")
    stop_file = tmp_path / "emergency_stop"
    stop_file.write_text("requested", encoding="utf-8")
    monkeypatch.setattr(config, "EMERGENCY_STOP_FILE", stop_file)
    monkeypatch.setattr(config, "TRADING_MODE", "live")
    monkeypatch.setattr(config, "LIQUIDATION_POSITIONS_FETCH_RETRIES", 3)
    monkeypatch.setattr(config, "LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS", 0)
    sleep_calls = []
    monkeypatch.setattr(trading_usecase_module.time, "sleep", sleep_calls.append)

    responses = iter([
        [],  # run()開始時の口座状態取得(残高基準の計算用)で消費される
        None,
        None,
        [{"Symbol": "7203", "Side": config.OrderSide.SELL.value, "HoldQty": 100}],
        [],  # ループ終了後の最終リフレッシュ(レポート用)で消費される
    ])

    class PositionsClient:
        def get_positions(self, token):
            return next(responses)

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 92.0}

    orders = []

    class OrderSender:
        def set_price(self, symbol, price):
            pass

        def place_market_order(self, token, symbol, side, quantity):
            orders.append((symbol, side, quantity))
            return {"Result": 0, "OrderId": "emergency-1"}

    messages = []
    use_case = _use_case(
        tmp_path, messages.append, PositionsClient(), board_client=BoardClient(), order_sender=OrderSender()
    )

    use_case.run(top_symbols_path=symbols_path, sleep=lambda seconds: None)

    assert orders == [("7203", config.OrderSide.SELL.value, 100)]
    assert len(sleep_calls) == 2  # 2回失敗した後に成功
    assert not any("強制決済" in m and "取得に失敗" in m for m in messages)


def test_fetch_positions_for_liquidation_distinguishes_failure_from_empty(monkeypatch, tmp_path):
    class EmptyPositionsClient:
        def get_positions(self, token):
            return []

    use_case = _use_case(tmp_path, lambda message: None, EmptyPositionsClient())
    assert use_case._fetch_positions_for_liquidation() == []

    class FailingPositionsClient:
        def get_positions(self, token):
            return None

    sleep_calls = []
    monkeypatch.setattr(trading_usecase_module.time, "sleep", sleep_calls.append)
    use_case = _use_case(tmp_path, lambda message: None, FailingPositionsClient())
    assert use_case._fetch_positions_for_liquidation() is None
    assert sleep_calls == [
        config.LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS
    ] * (config.LIQUIDATION_POSITIONS_FETCH_RETRIES - 1)
