from datetime import date
from types import SimpleNamespace

import pytest

from src.application.midday_filtering_usecase import MiddayBandSpec, MiddayRunReport, SymbolOutcome
from src.application.midday_verification_summary import format_verification_summary, summarize_verification
from src.domain.models import MinuteBar
from src.entrypoints import run_midday_filtering
from src.infrastructure import execution_lock
from src.infrastructure.market_data.get_intraday_bars import IntradayFetchResult
from src.infrastructure.persistence.filtering_diagnostics_repository import FilteringDiagnosticsRepository

DAY = date(2026, 10, 8)
TODAY = DAY.isoformat()


class FakeScreening:
    def load_for_date(self, target_date):
        return SimpleNamespace(symbols=["A", "B"])


class FakeVolume:
    def get_average_turnover_before(self, symbol, target_date, days):
        return 1_000_000.0


def _bars():
    return [MinuteBar(f"{TODAY}T{m}", 100.0, None, 1000.0, "yahoo") for m in ("09:00", "09:10", "09:29")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    prod = tmp_path / "prod"
    verify = tmp_path / "verify"
    cfg = run_midday_filtering.config
    monkeypatch.setattr(cfg, "MIDDAY_FILTERING_RESULT_ROOT", prod / "midday")
    monkeypatch.setattr(cfg, "MINUTE_BAR_PARQUET_DIR", prod / "bars")
    monkeypatch.setattr(cfg, "FILTERING_DIAGNOSTICS_DIRECTORY", prod / "board_diag")
    monkeypatch.setattr(cfg, "MIDDAY_FILTERING_VERIFY_RESULT_ROOT", verify / "midday")
    monkeypatch.setattr(cfg, "MINUTE_BAR_PARQUET_VERIFY_DIR", verify / "bars")
    monkeypatch.setattr(execution_lock, "MIDDAY_FILTERING_VERIFY_LOCK_FILE", tmp_path / ".verify.lock")
    monkeypatch.setattr(execution_lock, "MIDDAY_FILTERING_LOCK_FILE", tmp_path / ".midday.lock")
    monkeypatch.setattr(run_midday_filtering, "is_trading_day", lambda d: True)
    monkeypatch.setattr(run_midday_filtering, "YahooFinanceClient", lambda: FakeVolume())
    monkeypatch.setattr(run_midday_filtering, "CachedVolumeClient", lambda client: client)
    monkeypatch.setattr(
        run_midday_filtering, "fetch_yahoo_intraday_bars", lambda symbol, timeout=30: IntradayFetchResult(_bars(), True)
    )

    def fake_build_bands(root=None):
        root = cfg.MIDDAY_FILTERING_RESULT_ROOT if root is None else root
        return [MiddayBandSpec(
            name="450", price_cap=450.0, screening_repository=FakeScreening(),
            diagnostics_repository=FilteringDiagnosticsRepository(root / "450" / "diagnostics"),
            comparison_only=True,
        )]

    monkeypatch.setattr(run_midday_filtering, "build_bands", fake_build_bands)
    notices = []
    monkeypatch.setattr(run_midday_filtering, "notify_daily", notices.append)
    return SimpleNamespace(prod=prod, verify=verify, notices=notices)


def _files(path):
    return [p for p in path.rglob("*") if p.is_file()] if path.exists() else []


def test_verify_requires_date(env, capsys):
    with pytest.raises(SystemExit):
        run_midday_filtering.main(["--verify"])
    assert "--date" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--budget-seconds", "10"], ["--verify-save-bars"]])
def test_verify_only_options_rejected_without_verify(env, extra):
    with pytest.raises(SystemExit):
        run_midday_filtering.main(["--date", TODAY, *extra])


def test_verify_writes_only_to_verify_dirs_and_sends_no_notification(env, capsys):
    run_midday_filtering.main(["--verify", "--date", TODAY])

    assert _files(env.prod) == []
    assert any(p.suffix == ".json" for p in _files(env.verify / "midday" / TODAY))
    assert env.notices == []
    out = capsys.readouterr().out
    assert "検証用実行の要約" in out and "本番の予算" in out
    # --verify-save-bars なしなら分足は保存しない
    assert _files(env.verify / "bars") == []


def test_verify_skip_does_not_notify(env, monkeypatch, capsys):
    monkeypatch.setattr(run_midday_filtering, "is_trading_day", lambda d: False)
    run_midday_filtering.main(["--verify", "--date", TODAY])
    assert env.notices == []
    assert "休場日" in capsys.readouterr().out


def test_verify_save_bars_writes_to_verify_parquet_only(env):
    run_midday_filtering.main(["--verify", "--date", TODAY, "--verify-save-bars"])
    assert _files(env.verify / "bars")
    assert _files(env.prod) == []


def test_budget_seconds_is_used_and_default_is_long(env, monkeypatch):
    deadlines = []

    class StubUseCase:
        def run(self, bands, target_date, deadline):
            deadlines.append(deadline - run_midday_filtering.monotonic())
            return MiddayRunReport(skipped_reason="stub")

    monkeypatch.setattr(run_midday_filtering, "_build_use_case", lambda **kwargs: StubUseCase())
    run_midday_filtering.main(["--verify", "--date", TODAY, "--budget-seconds", "123"])
    run_midday_filtering.main(["--verify", "--date", TODAY])
    assert deadlines[0] == pytest.approx(123, abs=2)
    assert deadlines[1] == pytest.approx(run_midday_filtering.DEFAULT_VERIFY_BUDGET_SECONDS, abs=2)
    assert deadlines[1] > 25 * 60 * 4


def test_verify_lock_does_not_conflict_with_production_midday_lock(env):
    with execution_lock.midday_filtering_lock() as production:
        assert production is True
        with execution_lock.midday_filtering_verify_lock() as verify:
            assert verify is True
            with execution_lock.midday_filtering_verify_lock() as second:
                assert second is False


def test_summary_judges_fit_in_production_budget():
    outcomes = {
        "A": SymbolOutcome("A", window={"bar_count": 30}, fetch_ms=2000),
        "B": SymbolOutcome("B", window={"bar_count": 5}, fetch_ms=4000),
        "C": SymbolOutcome("C", reason_code="MIDDAY_FETCH_FAILED", fetch_ms=30000),
        "D": SymbolOutcome("D", reason_code="MIDDAY_NO_BARS", window={"bar_count": 0}, fetch_ms=1000),
    }
    report = MiddayRunReport(fetched_count=4, unique_symbol_count=4, elapsed_seconds=600.0, outcomes=outcomes)

    summary = summarize_verification(report, 1500.0)

    assert summary["fetch_failed_count"] == 1 and summary["fetch_failed_rate"] == 0.25
    assert summary["per_symbol_median_seconds"] == 3.0 and summary["per_symbol_max_seconds"] == 30.0
    assert summary["bar_count_distribution"]["30本以上"] == 1
    assert summary["bar_count_distribution"]["1〜9本"] == 1 and summary["bar_count_distribution"]["0本"] == 1
    assert summary["fits_in_production_budget"] is True and summary["margin_seconds"] == 900.0
    assert any("収まる: はい" in line for line in format_verification_summary(summary))

    over = summarize_verification(MiddayRunReport(elapsed_seconds=1600.0), 1500.0)
    assert over["fits_in_production_budget"] is False
    assert any("収まる: いいえ" in line for line in format_verification_summary(over))
