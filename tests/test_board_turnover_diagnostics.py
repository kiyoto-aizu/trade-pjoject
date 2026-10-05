import json
from datetime import date
from types import SimpleNamespace

from src.application.filtering_usecase import FilteringUseCase
from src.entrypoints import diagnose_board_turnover
from src.infrastructure.kabu.board_repository import BoardRepository
from src.infrastructure.kabu.registration_aware_board_cache import RegistrationAwareBoardCache


class _ScreeningRepo:
    def __init__(self, symbols):
        self.symbols = symbols

    def load_for_date(self, _day):
        return SimpleNamespace(symbols=self.symbols)


class _ResultRepo:
    def save(self, _result):
        pass


class _VolumeStub:
    def get_average_turnover(self, _symbol, _days):
        return 100.0


def _run(board_client, symbols):
    saved = []
    diagnostics = SimpleNamespace(save=saved.append)
    result = FilteringUseCase(
        _ScreeningRepo(symbols), board_client, _VolumeStub(), _ResultRepo(),
        diagnostics_repository=diagnostics,
    ).execute(target_date=date(2026, 10, 5))
    return result, saved[0]


class _FakeApiBoards:
    """BoardRepository相当(診断用の生返答つき)。"""

    def __init__(self, boards):
        self.boards = boards
        self.calls = []

    def get_current_board(self, symbol):
        raise AssertionError("診断用メソッドが使われるはず")

    def get_current_board_for_diagnostics(self, symbol):
        self.calls.append(symbol)
        return self.boards[symbol]


def _board(volume=None, value=None, price=500.0):
    return {
        "symbol_name": "x", "current_price": price, "current_price_time": "2026-10-05T09:01:00+09:00",
        "current_price_status": 1, "trading_volume": volume, "trading_value": value,
        "response_keys": ["CurrentPrice", "SymbolName"], "trading_volume_time": None, "vwap": None,
        "raw_trading_volume": volume, "raw_trading_value": value,
    }


def test_missing_turnover_records_raw_board_and_fetch_meta():
    cache = RegistrationAwareBoardCache(
        _FakeApiBoards({"1111": _board(), "2222": _board(price=None), "3333": _board(volume=10, value=5000.0)}),
        lambda: {},
    )
    _, saved = _run(cache, ["1111", "2222", "3333"])
    records = {r["symbol"]: r for r in saved["candidates"]}

    assert records["1111"]["reason_code"] == "FILTER_TURNOVER_MISSING"
    assert records["1111"]["board_response_keys"] == ["CurrentPrice", "SymbolName"]
    assert records["1111"]["board_trading_volume"] is None
    assert records["1111"]["board_trading_value"] is None
    assert records["1111"]["board_current_price_status"] == 1
    assert records["1111"]["board_fetch_seq"] == 1
    assert records["1111"]["requests_since_clear"] == 0
    assert "seconds_since_last_clear" in records["1111"]
    assert records["2222"]["reason_code"] == "FILTER_TURNOVER_MISSING"
    assert records["2222"]["board_current_price_time"] == "2026-10-05T09:01:00+09:00"
    assert records["3333"]["board_fetch_seq"] == 3
    assert records["3333"]["requests_since_clear"] == 2
    assert "board_response_keys" not in records["3333"]
    assert saved["summary"]["board_missing_fetch_seq_distribution"] == {"1-10": 2}


def test_board_none_records_diagnostic_keys():
    class NoneBoard:
        def get_current_board(self, _symbol):
            return None

    cache = RegistrationAwareBoardCache(NoneBoard(), lambda: {})
    _, saved = _run(cache, ["1111"])
    record = saved["candidates"][0]

    assert record["reason_code"] == "FILTER_BOARD_FETCH_FAILED"
    assert record["board_response_keys"] is None
    assert record["board_trading_volume"] is None
    assert record["board_fetch_seq"] == 1


def test_cache_hit_does_not_advance_seq_and_clear_resets_counter():
    clears = []
    fake = _FakeApiBoards({s: _board(value=1.0) for s in ("1", "2", "3")})
    cache = RegistrationAwareBoardCache(fake, lambda: clears.append(1) or {}, batch_size=2)

    cache.get_current_board("1")
    cache.get_current_board("1")
    cache.get_current_board("2")
    assert cache.get_fetch_meta("1")["board_fetch_seq"] == 1
    assert cache.get_fetch_meta("2")["board_fetch_seq"] == 2
    assert cache.requests_since_clear == 2
    cache.get_current_board("3")

    assert clears == [1]
    assert cache.get_fetch_meta("3")["board_fetch_seq"] == 3
    assert cache.get_fetch_meta("3")["requests_since_clear"] == 0
    assert cache.requests_since_clear == 1
    assert cache.seconds_since_last_clear >= 0


def test_plain_board_client_keeps_results_without_meta():
    class Plain:
        def get_current_board(self, _symbol):
            return {"current_price": 100.0, "trading_value": 500.0, "trading_volume": 5}

    result, saved = _run(Plain(), ["1111"])
    assert result.symbols == ["1111"]
    assert "board_fetch_seq" not in saved["candidates"][0]
    assert saved["summary"]["board_missing_fetch_seq_distribution"] == {}


def test_board_repository_keeps_get_current_board_shape(monkeypatch):
    response = {
        "SymbolName": "n", "CurrentPrice": 1.0, "TradingVolume": 0, "TradingValue": None,
        "VWAP": 2.0, "TradingVolumeTime": "t",
    }
    monkeypatch.setattr(
        "src.infrastructure.kabu.board_repository.request_handler.send_get", lambda *a, **k: response
    )
    repo = BoardRepository("tok")
    assert set(repo.get_current_board("1")) == {
        "symbol_name", "current_price", "trading_volume", "trading_value", "response_keys"
    }
    full = repo.get_current_board_for_diagnostics("1")
    assert full["raw_trading_volume"] == 0
    assert full["raw_trading_value"] is None
    assert full["vwap"] == 2.0
    assert full["trading_volume_time"] == "t"


def test_diagnose_script_prints_table_and_saves_json(tmp_path, capsys):
    counts = {}

    class Client:
        def get_current_board_with_freshness(self, symbol):
            counts[symbol] = counts.get(symbol, 0) + 1
            return {
                "current_price": 100.0, "response_keys": ["a"], "raw_trading_volume": counts[symbol] * 10,
                "raw_trading_value": None, "trading_volume_time": None, "vwap": None,
            }

    output = tmp_path / "out.json"
    code = diagnose_board_turnover.main(
        ["--symbols", "1111,2222", "--repeat", "2", "--output", str(output), "--force"],
        client=Client(),
    )
    printed = capsys.readouterr().out
    data = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert "1111" in printed and "売買高が変化: 2件" in printed
    assert [r["seq"] for r in data["rows"]] == [1, 2, 3, 4]
    assert data["rows"][2]["registered_count"] == 2
    assert data["summary"][0]["volume_changed"] is True
