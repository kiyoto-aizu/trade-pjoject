import json
from datetime import date

from src.entrypoints.run_minute_backfill import collect_recent_symbols


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
