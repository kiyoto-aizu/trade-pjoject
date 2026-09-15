import json
import sys
from datetime import date

import requests

from src.entrypoints import run_daily_diary
from src.infrastructure.analysis.diary_context import build_diary_context
from src.infrastructure.analysis.diary_writer import NoteDiaryWriter


def test_build_diary_context_aggregates_sources_and_manual_notes(tmp_path):
    reports = tmp_path / "reports"
    backtests = tmp_path / "backtests"
    notes = tmp_path / "diary_notes"
    reports.mkdir()
    backtests.mkdir()
    notes.mkdir()
    (reports / "2026-09-15.json").write_text(
        json.dumps({"date": "2026-09-15", "order_count": 2, "total_profit_loss": 100}), encoding="utf-8"
    )
    (backtests / "latest_timeseries_20260915.json").write_text(
        json.dumps({"generated_at": "2026-09-15T18:00:00", "total_pnl": 250, "total_trades": 3}), encoding="utf-8"
    )
    (notes / "2026-09-15.md").write_text("手入力の気づき", encoding="utf-8")

    context = build_diary_context(
        date(2026, 9, 15), date(2026, 9, 16), reports, backtests, notes,
        git_log_provider=lambda start, end: ["日記機能を追加"],
    )

    assert context["period_start"] == "2026-09-15"
    assert context["period_end"] == "2026-09-16"
    assert context["commits"] == ["日記機能を追加"]
    assert context["daily_reports"][0]["order_count"] == 2
    assert context["backtest_runs"][0]["total_pnl"] == 250
    assert context["manual_notes"] == "2026-09-15: 手入力の気づき"


def test_note_diary_writer_returns_content_and_handles_request_failure(monkeypatch):
    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "## 今日やったこと\n検証しました。"}}]}

    monkeypatch.setattr("src.infrastructure.analysis.diary_writer.requests.post", lambda *args, **kwargs: DummyResponse())
    writer = NoteDiaryWriter("key", "model", "https://example.test")

    assert writer.write({"manual_notes": "メモ"}).startswith("## 今日やったこと")

    def raise_request_error(*args, **kwargs):
        raise requests.RequestException("network error")

    monkeypatch.setattr("src.infrastructure.analysis.diary_writer.requests.post", raise_request_error)
    assert writer.write({}) is None


def test_main_writes_markdown_when_writer_succeeds(monkeypatch, tmp_path):
    class DummyWriter:
        def write(self, context):
            return "## 今日やったこと\n日記本文です。"

    monkeypatch.setattr(run_daily_diary, "create_diary_writer", lambda: DummyWriter())
    monkeypatch.setattr(run_daily_diary, "build_diary_context", lambda *args: {"manual_notes": ""})
    monkeypatch.setattr(
        sys, "argv", ["run_daily_diary", "--date", "2026-09-16", "--output", str(tmp_path)]
    )

    run_daily_diary.main()

    output = tmp_path / "2026-09-16.md"
    assert output.exists()
    assert output.read_text(encoding="utf-8") == "# 2026-09-16 開発・トレード日記\n\n## 今日やったこと\n日記本文です。\n"