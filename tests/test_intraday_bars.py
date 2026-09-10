from src.infrastructure.market_data.get_intraday_bars import get_yahoo_intraday_bars


def test_get_yahoo_intraday_bars_parses_response_into_minute_bars(monkeypatch):
    fake_response = {
        "chart": {
            "result": [
                {
                    "timestamp": [1757480460, 1757480520],  # JSTで分単位の連続する2時刻
                    "indicators": {
                        "quote": [
                            {"close": [100.0, 101.0], "volume": [500, 300]},
                        ]
                    },
                }
            ]
        }
    }
    monkeypatch.setattr(
        "src.infrastructure.market_data.get_intraday_bars.request_handler.send_get",
        lambda *args, **kwargs: fake_response,
    )

    bars = get_yahoo_intraday_bars("7203", days=7)

    assert len(bars) == 2
    assert all(bar.source == "yahoo" for bar in bars)
    assert bars[0].price == 100.0
    assert bars[0].volume == 500
    assert bars[0].cumulative_volume is None
    assert bars[1].price == 101.0


def test_get_yahoo_intraday_bars_skips_null_closes(monkeypatch):
    fake_response = {
        "chart": {
            "result": [
                {
                    "timestamp": [1757480460, 1757480520],
                    "indicators": {
                        "quote": [
                            {"close": [None, 101.0], "volume": [None, 300]},
                        ]
                    },
                }
            ]
        }
    }
    monkeypatch.setattr(
        "src.infrastructure.market_data.get_intraday_bars.request_handler.send_get",
        lambda *args, **kwargs: fake_response,
    )

    bars = get_yahoo_intraday_bars("7203", days=7)

    assert len(bars) == 1
    assert bars[0].price == 101.0


def test_get_yahoo_intraday_bars_returns_empty_list_when_request_fails(monkeypatch):
    monkeypatch.setattr(
        "src.infrastructure.market_data.get_intraday_bars.request_handler.send_get",
        lambda *args, **kwargs: None,
    )

    assert get_yahoo_intraday_bars("7203", days=7) == []
