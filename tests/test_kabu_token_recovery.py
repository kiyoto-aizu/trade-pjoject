"""
kabuステーションAPIの401応答時トークン自動再取得・再試行と、関連する通知のテスト。
実際のAPI・本番の状態ファイル・DB・ログには触れない。
"""
import contextlib
import json
import sys
from datetime import date, datetime
from types import SimpleNamespace

import pytest
import requests

from src.api import request_handler
from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.infrastructure.kabu.token_provider import (
    TokenProvider,
    get_token_provider,
    reset_token_provider_for_tests,
)


class FakeResponse:
    """requests.Responseの最小限のフェイク。"""

    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self.text = f"status={status_code}"
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _reset_token_provider():
    reset_token_provider_for_tests()
    request_handler._last_request_at = None
    yield
    reset_token_provider_for_tests()


# ================================================================================
# request_handler: 401応答時の再取得・再試行
# ================================================================================

def test_401_then_200_refreshes_token_once_and_retries_successfully(monkeypatch):
    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    attempts = []

    def fake_get(url, headers=None, params=None, timeout=10):
        attempts.append(headers["X-API-KEY"])
        if headers["X-API-KEY"] == "old-token":
            return FakeResponse(401)
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr(request_handler.requests, "get", fake_get)
    fetch_calls = []
    reset_token_provider_for_tests(
        TokenProvider(fetch_token=lambda: fetch_calls.append(1) or "new-token")
    )

    result = request_handler.send_get(
        "http://localhost:18081/kabusapi/board/7203@1", headers={"X-API-KEY": "old-token"}
    )

    assert result == {"ok": True}
    assert attempts == ["old-token", "new-token"]
    assert len(fetch_calls) == 1


def test_persistent_401_is_treated_as_failure_without_infinite_loop(monkeypatch):
    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    attempts = []

    def fake_get(url, headers=None, params=None, timeout=10):
        attempts.append(headers["X-API-KEY"])
        return FakeResponse(401)

    monkeypatch.setattr(request_handler.requests, "get", fake_get)
    fetch_calls = []
    provider = TokenProvider(fetch_token=lambda: fetch_calls.append(1) or "new-token")
    reset_token_provider_for_tests(provider)

    result = request_handler.send_get(
        "http://localhost:18081/kabusapi/board/7203@1", headers={"X-API-KEY": "old-token"}
    )

    assert result is None
    # 元の呼び出し + 再試行1回のみ（無限ループにならない）
    assert attempts == ["old-token", "new-token"]
    assert len(fetch_calls) == 1
    assert provider.recovery_failed is True


def test_min_refresh_interval_reuses_cached_token_without_refetching(monkeypatch):
    import src.infrastructure.kabu.token_provider as token_provider_module

    fetch_calls = []
    provider = TokenProvider(
        fetch_token=lambda: fetch_calls.append(1) or f"token-{len(fetch_calls)}"
    )
    reset_token_provider_for_tests(provider)

    now_box = {"t": 1000.0}
    monkeypatch.setattr(token_provider_module.time, "monotonic", lambda: now_box["t"])
    monkeypatch.setattr(config, "KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", 60)

    token1 = provider.recover_from_unauthorized()
    assert token1 == "token-1"
    assert len(fetch_calls) == 1

    now_box["t"] += 1  # 間隔内
    token2 = provider.recover_from_unauthorized()
    assert token2 == "token-1"
    assert len(fetch_calls) == 1

    now_box["t"] += 61  # 間隔を超えた
    token3 = provider.recover_from_unauthorized()
    assert token3 == "token-2"
    assert len(fetch_calls) == 2


def test_non_401_error_does_not_trigger_token_refresh(monkeypatch):
    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    monkeypatch.setattr(
        request_handler.requests, "get", lambda *a, **k: FakeResponse(500)
    )
    fetch_calls = []
    reset_token_provider_for_tests(
        TokenProvider(fetch_token=lambda: fetch_calls.append(1) or "new-token")
    )

    result = request_handler.send_get(
        "http://localhost:18081/kabusapi/board/7203@1", headers={"X-API-KEY": "old-token"}
    )

    assert result is None
    assert fetch_calls == []


def test_token_endpoint_401_is_not_retried(monkeypatch):
    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=10):
        calls.append(url)
        return FakeResponse(401)

    monkeypatch.setattr(request_handler.requests, "post", fake_post)
    fetch_calls = []
    reset_token_provider_for_tests(
        TokenProvider(fetch_token=lambda: fetch_calls.append(1) or "new-token")
    )

    result = request_handler.send_post(
        "http://localhost:18081/kabusapi/token",
        data={"APIPassword": "x"},
        headers={"Content-Type": "application/json"},
    )

    assert result is None
    assert len(calls) == 1  # 再試行していない
    assert fetch_calls == []


def test_token_provider_is_shared_across_multiple_kabu_clients(monkeypatch):
    from src.infrastructure.kabu.board_repository import BoardRepository
    from src.infrastructure.kabu.regulation_repository import RegulationRepository

    monkeypatch.setattr(request_handler, "_wait_for_request_slot", lambda: None)

    def fake_get(url, headers=None, params=None, timeout=10):
        if headers["X-API-KEY"] == "old-token":
            return FakeResponse(401)
        if "board" in url:
            return FakeResponse(200, {"CurrentPrice": "100"})
        return FakeResponse(200, {"RegulationsInfo": []})

    monkeypatch.setattr(request_handler.requests, "get", fake_get)
    fetch_calls = []
    reset_token_provider_for_tests(
        TokenProvider(fetch_token=lambda: fetch_calls.append(1) or "shared-new-token")
    )

    board = BoardRepository("old-token")
    regulation = RegulationRepository("old-token")

    assert board.get_current_price("7203") == 100.0
    assert regulation.get_regulation("7203", 1).is_restricted is False
    # 複数クライアントにまたがっても、共有のトークン提供者経由で再取得は1回だけ
    assert len(fetch_calls) == 1
    assert get_token_provider().get_token() == "shared-new-token"


# ================================================================================
# TradingUseCase: 通知（1runにつき1回）
# ================================================================================

def _base_use_case(tmp_path, notifier, board_client, positions):
    class MarketDataClient:
        def get_yahoo_daily_closes(self, symbol):
            return [100.0] * 5

    class PositionsClient:
        def get_positions(self, token):
            return positions

    class WalletClient:
        def get_wallet_cash(self, token):
            return {"StockAccountWallet": 100_000}

    return TradingUseCase(
        token="dummy",
        order_history_path=tmp_path / "order_history.json",
        market_data_client=MarketDataClient(),
        board_client=board_client,
        wallet_client=WalletClient(),
        positions_client=PositionsClient(),
        notifier=notifier,
        daily_analyzer=SimpleNamespace(analyze=lambda summary: None),
        daily_report_directory=tmp_path / "reports",
        kill_switch_baseline_path=tmp_path / "kill_switch_baseline.json",
    )


def test_board_fetch_consecutive_failures_notify_once(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "MARKET_CLOSE_HOUR", 10)
    monkeypatch.setattr(config, "MARKET_CLOSE_MINUTE", 3)
    monkeypatch.setattr(config, "ALLOW_OVERNIGHT_HOLDING", True)
    monkeypatch.setattr(config, "BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(
        "src.application.trading_usecase.calculate_price_limit",
        lambda closes: SimpleNamespace(lower_band=80.0, upper_band=99.0),
    )
    monkeypatch.setattr("src.application.trading_usecase.calculate_rsi", lambda *a, **k: 60.0)

    class BoardClient:
        def get_current_board(self, token, symbol):
            return None  # 板取得が常に失敗する

    messages = []
    positions = [{"Symbol": "7203", "Side": config.OrderSide.SELL.value, "HoldQty": 100}]
    use_case = _base_use_case(tmp_path, messages.append, BoardClient(), positions)

    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")

    times = [
        datetime(2026, 10, 2, 10, 0),
        datetime(2026, 10, 2, 10, 1),
        datetime(2026, 10, 2, 10, 2),
        datetime(2026, 10, 2, 10, 3),
    ]
    tick_index = [0]
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: times[tick_index[0]],
        sleep=lambda seconds: tick_index.__setitem__(0, tick_index[0] + 1),
    )

    failure_notifications = [m for m in messages if "板取得" in m]
    assert len(failure_notifications) == 1
    assert "3回連続" in failure_notifications[0]
    assert "7203" in failure_notifications[0]


def test_auth_recovery_failure_notifies_once_per_run(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RSI_PERIOD", 2)
    monkeypatch.setattr(config, "RSI_MINIMUM_CLOSES", 5)
    monkeypatch.setattr(config, "MARKET_CLOSE_HOUR", 10)
    monkeypatch.setattr(config, "MARKET_CLOSE_MINUTE", 2)
    monkeypatch.setattr(config, "ALLOW_OVERNIGHT_HOLDING", True)
    monkeypatch.setattr(
        "src.application.trading_usecase.calculate_price_limit",
        lambda closes: SimpleNamespace(lower_band=80.0, upper_band=99.0),
    )
    monkeypatch.setattr("src.application.trading_usecase.calculate_rsi", lambda *a, **k: 60.0)

    class BoardClient:
        def get_current_board(self, token, symbol):
            return {"current_price": 100.0}

    provider = TokenProvider(fetch_token=lambda: None)
    provider.report_retry_still_unauthorized()
    reset_token_provider_for_tests(provider)

    messages = []
    positions = [{"Symbol": "7203", "Side": config.OrderSide.SELL.value, "HoldQty": 100}]
    use_case = _base_use_case(tmp_path, messages.append, BoardClient(), positions)

    symbols_path = tmp_path / "top_symbols.json"
    symbols_path.write_text(json.dumps(["7203"]), encoding="utf-8")

    times = [
        datetime(2026, 10, 2, 10, 0),
        datetime(2026, 10, 2, 10, 1),
        datetime(2026, 10, 2, 10, 2),
    ]
    tick_index = [0]
    use_case.run(
        top_symbols_path=symbols_path,
        now_provider=lambda: times[tick_index[0]],
        sleep=lambda seconds: tick_index.__setitem__(0, tick_index[0] + 1),
    )

    auth_notifications = [m for m in messages if "API認証" in m]
    assert len(auth_notifications) == 1
    assert "7203" in auth_notifications[0]


# ================================================================================
# 診断スクリプト: 取引プロセス稼働中は中止、--forceで上書き
# ================================================================================

def test_diagnostic_script_aborts_when_trading_process_is_running(monkeypatch):
    from scripts.ops import verify_liquidation_freshness_live as diag

    @contextlib.contextmanager
    def _lock_held():
        yield False

    monkeypatch.setattr(diag, "market_workflow_lock", _lock_held)
    token_calls = []
    monkeypatch.setattr(diag, "get_api_token", lambda: token_calls.append(1) or "token")
    monkeypatch.setattr(sys, "argv", ["verify_liquidation_freshness_live.py"])

    assert diag.main() == 1
    assert token_calls == []


def test_diagnostic_script_force_skips_lock_check(monkeypatch):
    from scripts.ops import verify_liquidation_freshness_live as diag

    def _should_not_be_called():
        raise AssertionError("market_workflow_lock should not be called when --force is set")

    monkeypatch.setattr(diag, "market_workflow_lock", _should_not_be_called)
    monkeypatch.setattr(diag, "get_api_token", lambda: None)
    monkeypatch.setattr(sys, "argv", ["verify_liquidation_freshness_live.py", "--force"])

    assert diag.main() == 1
