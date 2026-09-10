import json
from datetime import date, datetime
from pathlib import Path

from src.application.minute_bar_usecase import MinuteBarCollectionUseCase
from src.entrypoints.run_minute_backfill import collect_recent_symbols
from src.infrastructure.persistence.minute_bar_repository import MinuteBarRepository


class FakeBoardClient:
    def __init__(self, data: dict[str, dict]):
        self.data = data

    def get_current_board(self, symbol: str):
        return self.data.get(symbol)


def test_minute_bar_collection_collect_once(tmp_path):
    repo = MinuteBarRepository(tmp_path)
    client = FakeBoardClient({
        "7203": {"current_price": 2500.0, "trading_volume": 10000},
        "9999": {"current_price": 500.0, "trading_volume": 5000},
    })
    usecase = MinuteBarCollectionUseCase(client, repo)

    now = datetime(2026, 9, 10, 9, 30, 0)
    usecase.collect_once(["7203", "9999"], now)

    bars_7203 = repo.load_bars(date(2026, 9, 10), "7203")
    assert len(bars_7203) == 1
    assert bars_7203[0].price == 2500.0
    assert bars_7203[0].cumulative_volume == 10000.0
    assert bars_7203[0].volume is None
    assert bars_7203[0].source == "poll"

    # 2回目の収集で差分volumeが計算されることを確認
    client.data["7203"] = {"current_price": 2505.0, "trading_volume": 10500}
    now2 = datetime(2026, 9, 10, 9, 31, 0)
    usecase.collect_once(["7203"], now2)

    bars_7203 = repo.load_bars(date(2026, 9, 10), "7203")
    assert len(bars_7203) == 2
    assert bars_7203[1].price == 2505.0
    assert bars_7203[1].volume == 500.0
    assert bars_7203[1].source == "poll"


def test_collect_recent_symbols(tmp_path):
    filtering_dir = tmp_path / "filtering"
    filtering_dir.mkdir(parents=True)

    # 2026-09-08のフィルタリング結果
    file_1 = filtering_dir / "2026-09-08.json"
    file_1.write_text(json.dumps({
        "date": "2026-09-08",
        "symbols": ["7203", "8306"],
        "generated_at": "2026-09-08T09:30:00",
    }), encoding="utf-8")

    # 2026-09-10のフィルタリング結果
    file_2 = filtering_dir / "2026-09-10.json"
    file_2.write_text(json.dumps({
        "date": "2026-09-10",
        "symbols": ["8306", "9984"],
        "generated_at": "2026-09-10T09:30:00",
    }), encoding="utf-8")

    today = date(2026, 9, 10)
    # 直近3日間 (2026-09-07以降)
    symbols = collect_recent_symbols(filtering_dir, days=3, today=today)
    assert symbols == ["7203", "8306", "9984"]

    # 直近1日間 (2026-09-09以降)
    symbols_1d = collect_recent_symbols(filtering_dir, days=1, today=today)
    assert symbols_1d == ["8306", "9984"]
