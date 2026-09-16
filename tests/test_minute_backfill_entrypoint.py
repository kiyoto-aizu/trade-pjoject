import json
from datetime import date, datetime
from unittest.mock import patch

from src.domain.models import MinuteBar
from src.entrypoints.run_minute_backfill import main
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def test_run_minute_backfill_main_flow(tmp_path):
    data_dir = tmp_path / "data"
    filtering_dir = data_dir / "filtering"
    minute_bars_dir = data_dir / "minute_bars_parquet"
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
            patch("src.entrypoints.run_minute_backfill.notify_analysis") as mock_notify, \
         patch("sys.argv", ["run_minute_backfill", "--days", "7"]):
        
        main(
            now_provider=lambda: datetime(2026, 9, 10, 10, 0, 0),
            filtering_dir=filtering_dir,
            output_dir=minute_bars_dir,
        )

        mock_notify.assert_called_once()
        assert "【業務】市場データ管理\n【機能】分足バックフィル\n" in mock_notify.call_args[0][0]
        assert "対象銘柄数: 1件" in mock_notify.call_args[0][0]
        assert "取込本数: 1本" in mock_notify.call_args[0][0]

    repository = ParquetMinuteBarRepository(minute_bars_dir)
    saved_bars = repository.load_bars(date(2026, 9, 10), "7203")
    assert len(saved_bars) == 1
    assert saved_bars[0].source == "yahoo"
    assert (minute_bars_dir / "symbol=7203" / "date=2026-09-10" / "data.parquet").exists()
    assert not list((data_dir / "minute_bars").glob("**/*.json"))
