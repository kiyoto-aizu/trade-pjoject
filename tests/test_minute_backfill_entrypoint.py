import json
from datetime import datetime
from unittest.mock import patch

from src.domain.models import MinuteBar
from src.entrypoints.run_minute_backfill import main


def test_run_minute_backfill_main_flow(tmp_path):
    data_dir = tmp_path / "data"
    filtering_dir = data_dir / "filtering"
    minute_bars_dir = data_dir / "minute_bars"
    filtering_dir.mkdir(parents=True)

    # フィルタリング結果ファイル作成
    (filtering_dir / "2026-09-10.json").write_text(json.dumps({
        "date": "2026-09-10",
        "symbols": ["7203"],
        "generated_at": "2026-09-10T09:30:00",
    }), encoding="utf-8")

    fake_bars = [
        MinuteBar(time="2026-09-10T09:31:00", price=100.0, cumulative_volume=None, volume=500, source="yahoo"),
    ]

    with patch("src.entrypoints.run_minute_backfill.get_yahoo_intraday_bars", return_value=fake_bars), \
         patch("src.entrypoints.run_minute_backfill.send_line_notify") as mock_notify, \
         patch("sys.argv", ["run_minute_backfill", "--days", "7"]):
        
        main(
            now_provider=lambda: datetime(2026, 9, 10, 10, 0, 0),
            filtering_dir=filtering_dir,
            output_dir=minute_bars_dir,
        )

        mock_notify.assert_called_once()
        assert "【分足バックフィル結果】" in mock_notify.call_args[0][0]
        assert "対象銘柄数: 1" in mock_notify.call_args[0][0]
        assert "取り込んだ足の総数: 1" in mock_notify.call_args[0][0]

    saved_file = minute_bars_dir / "2026-09-10" / "7203.json"
    assert saved_file.exists()
    saved_data = json.loads(saved_file.read_text(encoding="utf-8"))
    assert len(saved_data["bars"]) == 1
    assert saved_data["bars"][0]["source"] == "yahoo"
