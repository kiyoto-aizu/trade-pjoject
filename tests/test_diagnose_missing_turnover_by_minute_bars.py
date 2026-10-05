import json

from src.domain.models import MinuteBar
from src.entrypoints import diagnose_missing_turnover_by_minute_bars as d


def _bar(time, price=100.0, volume=10.0):
    return MinuteBar(time=time, price=price, cumulative_volume=None, volume=volume, source="yahoo")


def test_parse_log_reads_utf16_and_separates_reasons(tmp_path):
    lines = [
        "2026-10-05 09:30:10 WARNING x: フィルタリング評価対象外: 銘柄=4564 理由=当日売買代金を計算できません",
        "2026-10-05 09:30:11 WARNING x: フィルタリング評価対象外: 銘柄=4564 理由=当日売買代金を計算できません",
        "2026-10-05 09:30:12 WARNING x: フィルタリング評価対象外: 銘柄=7074 理由=現在値なし",
        "2026-10-05 09:30:13 WARNING x: フィルタリング評価対象外: 銘柄=1111 理由=平均売買代金なし",
    ]
    path = tmp_path / "run_filtering_stderr_2026-10-05.log"
    path.write_bytes("\n".join(lines).encode("utf-16"))

    parsed = d.parse_log_missing(path)

    assert parsed == {"turnover": ["4564"], "current_price": ["7074"], "other": []}


def test_window_excludes_0930_and_includes_0900_and_0929():
    bars = [
        _bar("2026-10-05T08:59:00", volume=1000),
        _bar("2026-10-05T09:00:00", volume=10),
        _bar("2026-10-05T09:24:00", volume=20),
        _bar("2026-10-05T09:25:00", volume=30),
        _bar("2026-10-05T09:29:00", volume=40, price=200.0),
        _bar("2026-10-05T09:30:00", volume=5000),
        _bar("2026-10-02T09:10:00", volume=7000),
    ]

    window = d.aggregate_window(bars, "2026-10-05")

    assert window["volume"] == 100
    assert window["last_five_volume"] == 70
    assert window["value_estimate"] == 10 * 100 + 20 * 100 + 30 * 100 + 40 * 200
    assert window["bar_count"] == 4


def test_three_verdicts():
    traded = d.aggregate_window([_bar("2026-10-05T09:01:00", volume=1)], "2026-10-05")
    zero = d.aggregate_window([_bar("2026-10-05T09:01:00", volume=0), _bar("2026-10-05T09:02:00", volume=None)], "2026-10-05")
    none = d.aggregate_window([_bar("2026-10-05T10:00:00")], "2026-10-05")

    assert (d.judge(traded), d.judge(zero), d.judge(none)) == ("TRADED", "NO_VOLUME", "NO_BARS")


def test_fetch_failure_continues_and_unavailable_date_is_marked():
    def fetcher(symbol):
        if symbol == "1111":
            raise RuntimeError("boom")
        if symbol == "2222":
            return []
        return [_bar("2026-10-05T09:01:00", volume=5)]

    bars, errors = d.fetch_all(["1111", "2222", "3333"], fetcher)
    targets = {
        "2026-10-05": {"missing": ["1111", "2222", "3333"], "current_price_missing": [], "evaluated": []},
        "2026-09-29": {"missing": ["3333"], "current_price_missing": [], "evaluated": []},
    }
    report = d.build_report(targets, bars, errors, {})

    verdicts = {r["symbol"]: r["verdict"] for r in report["2026-10-05"]["rows"]}
    assert verdicts == {"1111": "FETCH_FAILED", "2222": "FETCH_FAILED", "3333": "TRADED"}
    assert report["2026-10-05"]["counts"] == {"FETCH_FAILED": 2, "TRADED": 1}
    assert report["2026-09-29"]["day_available"] is False
    assert report["2026-09-29"]["rows"][0]["verdict"] == "UNAVAILABLE"


def test_main_uses_diagnostics_for_evaluated_comparison_and_saves_json(tmp_path, capsys):
    log_dir, diag_dir = tmp_path / "logs", tmp_path / "diag"
    log_dir.mkdir()
    diag_dir.mkdir()
    (log_dir / "run_filtering_stderr_2026-10-05.log").write_bytes(
        "フィルタリング評価対象外: 銘柄=4564 理由=当日売買代金を計算できません".encode("utf-16")
    )
    (diag_dir / "2026-10-05_093103.json").write_text(json.dumps({
        "date": "2026-10-05",
        "candidates": [
            {"symbol": "4564", "status": "skipped", "reason_code": "FILTER_TURNOVER_MISSING"},
            {"symbol": "7074", "status": "skipped", "reason_code": "FILTER_CURRENT_PRICE_MISSING"},
            {"symbol": "9999", "status": "evaluated", "numerator": 1000.0},
        ],
    }), encoding="utf-8")
    calls = []

    def fetcher(symbol):
        calls.append(symbol)
        return [_bar("2026-10-05T09:01:00", price=100.0, volume=10.0)]

    out = tmp_path / "out.json"
    code = d.main(["--dates", "2026-10-05", "--no-simulation"], fetcher=fetcher, log_directory=log_dir,
                  diagnostics_directory=diag_dir, output=out)

    data = json.loads(out.read_text(encoding="utf-8"))
    rows = {r["symbol"]: r for r in data["report"]["2026-10-05"]["rows"]}
    assert code == 0
    assert sorted(calls) == ["4564", "7074", "9999"]
    assert rows["7074"]["group"] == "current_price_missing"
    assert rows["9999"]["yahoo_to_kabu_value_ratio"] == 1.0
    assert data["report"]["2026-10-05"]["counts"] == {"TRADED": 2}
    assert "ログ1件 / 診断JSON2件 / 一致=False" in capsys.readouterr().out


def _ratios(n_missing_top=True):
    return {"A": 3.0, "B": 2.0, "C": 1.5, "D": 0.5, "E": 0.2}


def test_simulate_day_ranks_overlap_and_excludes_unestimable():
    ratios = {"A": 3.0, "B": 2.0, "C": 1.5, "D": 0.5, "E": 0.2}
    result = d.simulate_day(ratios, {"Z": "分足なし"}, adopted=["B", "C", "D", "Z"], missing={"A", "E"}, top_n=3)

    assert [r["symbol"] for r in result["estimated_top"]] == ["A", "B", "C"]
    assert result["top_missing_symbols"] == ["A"]
    assert result["overlap_with_adopted"] == 2
    assert result["adopted_unestimable"] == ["Z"]
    assert result["adopted_ratio_stats"]["median"] == 1.5
    assert result["missing_ratio_stats"]["max"] == 3.0
    assert (result["over_one_evaluated"], result["over_one_missing"]) == (2, 1)
    assert result["unestimable_count"] == 1


def test_estimate_ratios_excludes_missing_bars_and_average():
    day = "2026-10-05"
    bars = {
        "A": [_bar(f"{day}T09:01:00", price=100.0, volume=10.0)],
        "B": [_bar(f"{day}T09:01:00", volume=10.0)],
        "C": [_bar("2026-10-02T09:01:00", volume=10.0)],
    }
    turnover = {"A": {"2026-10-02": 500.0, "2026-10-05": 99999.0}, "C": {"2026-10-02": 500.0}}

    ratios, unestimable = d.estimate_ratios(day, ["A", "B", "C", "D"], bars, turnover)

    assert ratios == {"A": 2.0}
    assert unestimable == {"B": "平均売買代金なし", "C": "当日の分足なし", "D": "分足なし"}


def test_load_population_falls_back_to_diagnostics_and_notes_it(tmp_path):
    diag = tmp_path / "diag"
    diag.mkdir()
    (diag / "2026-10-05_093103.json").write_text(json.dumps({
        "date": "2026-10-05",
        "candidates": [{"symbol": "1", "selected": True}, {"symbol": "2", "selected": False}],
    }), encoding="utf-8")

    fallback = d.load_population("2026-10-05", tmp_path / "no", tmp_path / "no", diag)
    assert fallback["population"] == ["1", "2"] and fallback["adopted"] == ["1"]
    assert fallback["source"].startswith("FALLBACK")
    assert d.load_population("2026-10-01", tmp_path / "no", tmp_path / "no", diag) is None

    screening, filtering = tmp_path / "s", tmp_path / "f"
    screening.mkdir()
    filtering.mkdir()
    (screening / "2026-10-02.json").write_text(json.dumps({"symbols": ["7", "8"]}), encoding="utf-8")
    (filtering / "2026-10-05.json").write_text(json.dumps({"symbols": ["7"]}), encoding="utf-8")
    normal = d.load_population("2026-10-05", screening, filtering, diag)
    assert normal["population"] == ["7", "8"] and normal["adopted"] == ["7"]
    assert "FALLBACK" not in normal["source"]


def test_calibrate_separates_zero_yahoo_value():
    result = d.calibrate({"A": 0.9, "B": 0.0}, {"A": 1.0, "B": 1.0, "C": 1.0}, {"A": 10.0, "B": 0})
    assert result["stats"]["median"] == 0.9
    assert result["zero_yahoo_value_symbols"] == ["B"]


def test_run_simulation_continues_on_fetch_failure_and_marks_unavailable_day(tmp_path):
    screening, filtering = tmp_path / "s", tmp_path / "f"
    screening.mkdir()
    filtering.mkdir()
    (screening / "2026-10-02.json").write_text(json.dumps({"symbols": ["A", "B", "C"]}), encoding="utf-8")
    (filtering / "2026-10-05.json").write_text(json.dumps({"symbols": ["A"]}), encoding="utf-8")
    (screening / "2026-09-25.json").write_text(json.dumps({"symbols": ["A"]}), encoding="utf-8")
    (filtering / "2026-09-28.json").write_text(json.dumps({"symbols": ["A"]}), encoding="utf-8")

    def fetch(symbol):
        if symbol == "C":
            raise RuntimeError("x")
        return [_bar("2026-10-05T09:01:00", price=100.0, volume={"A": 10.0, "B": 40.0}[symbol])]

    sim = d.run_simulation(
        ["2026-09-28", "2026-10-05"], {"2026-10-05": {"missing": ["B"], "current_price_missing": []}},
        fetch, lambda s: {"2026-10-02": 1000.0}, screening, filtering, tmp_path / "diag",
    )

    assert "取得不可" in sim["days"]["2026-09-28"]["skipped"]
    day = sim["days"]["2026-10-05"]
    assert [r["symbol"] for r in day["estimated_top"]] == ["B", "A"]
    assert day["top_missing_symbols"] == ["B"]
    assert day["unestimable"] == {"C": "分足なし"}
    assert "C" in sim["fetch_errors"]
