import json
from datetime import date
from pathlib import Path

import pytest

from src.application.run_strategy_review_usecase import RunStrategyReviewUseCase
from src.entrypoints import run_strategy_review


def test_run_strategy_review_connects_outputs_and_notifies_once(tmp_path):
    notifications = []
    advisor_path = tmp_path / "advisor.md"
    run_directory = tmp_path / "verification" / "run-1"
    feedback_path = run_directory / "verified_hypotheses.json"
    calls = []

    def advisor(start, end):
        calls.append(("advisor", start, end))
        return {"output_path": advisor_path}

    def verifier(path):
        calls.append(("verifier", path))
        return {
            "run_directory": run_directory,
            "feedback_path": feedback_path,
            "results": [
                {"status": "判定済み", "verdict": "支持"},
                {"status": "判定済み", "verdict": "支持"},
                {"status": "判定済み", "verdict": "棄却"},
                {"status": "判定済み", "verdict": "追加データ必要"},
            ],
        }

    result = RunStrategyReviewUseCase(advisor, verifier, notifications.append).execute(
        date(2026, 9, 1), date(2026, 9, 25)
    )

    assert calls == [
        ("advisor", date(2026, 9, 1), date(2026, 9, 25)),
        ("verifier", advisor_path),
    ]
    assert result["hypothesis_count"] == 4
    assert result["judged_count"] == 4
    assert result["unverifiable_count"] == 0
    assert result["verdict_counts"] == {"支持": 2, "棄却": 1, "追加データ必要": 1}
    assert len(notifications) == 1
    assert "仮説4件生成、検証完了(判定済み4件: 支持2件/棄却1件/追加データ必要1件/検証不能0件)" in notifications[0]
    assert "仮説ファイル: " in notifications[0]
    assert "検証結果: " in notifications[0]
    assert "フィードバック: " in notifications[0]


def test_run_strategy_review_does_not_count_unverifiable_as_additional_data_needed(tmp_path):
    notifications = []
    use_case = RunStrategyReviewUseCase(
        lambda start, end: {"output_path": tmp_path / "advisor.md"},
        lambda path: {
            "run_directory": tmp_path / "run",
            "feedback_path": tmp_path / "run" / "verified_hypotheses.json",
            "results": [
                {"status": "判定済み", "verdict": "支持"},
                {"status": "検証不能", "verdict": "追加データ必要"},
            ],
        },
        notifications.append,
    )

    result = use_case.execute(date(2026, 9, 1), date(2026, 9, 25))

    assert result["judged_count"] == 1
    assert result["unverifiable_count"] == 1
    assert result["untested_count"] == 0
    assert result["verdict_counts"] == {"支持": 1, "棄却": 0, "追加データ必要": 0}
    assert "検証一部未完了(判定済み1件: 支持1件/棄却0件/追加データ必要0件/検証不能1件/未検証0件)" in notifications[0]


def test_run_strategy_review_reports_priority_exclusions_separately(tmp_path):
    notifications = []
    use_case = RunStrategyReviewUseCase(
        lambda start, end: {"output_path": tmp_path / "advisor.md"},
        lambda path: {
            "run_directory": tmp_path / "run",
            "feedback_path": tmp_path / "run" / "verified_hypotheses.json",
            "results": [
                {"status": "判定済み", "verdict": "支持"},
                {"status": "未検証(優先度中のため対象外)", "verdict": "未検証"},
            ],
        },
        notifications.append,
    )

    result = use_case.execute(date(2026, 9, 1), date(2026, 9, 25))

    assert result["unverifiable_count"] == 0
    assert result["untested_count"] == 1
    assert "検証不能0件/未検証1件" in notifications[0]


def test_run_strategy_review_summarizes_once_saves_full_text_and_notifies_short_text(tmp_path):
    notifications = []
    run_directory = tmp_path / "verification" / "run-1"
    run_directory.mkdir(parents=True)
    summary = "横断所見\n" + ("次アクション案 " * 180)
    calls = []

    def summarize(payload):
        calls.append(payload)
        return summary

    result = RunStrategyReviewUseCase(
        lambda start, end: {"output_path": tmp_path / "advisor.md"},
        lambda path: {
            "run_directory": run_directory,
            "feedback_path": run_directory / "verified_hypotheses.json",
            "results": [
                {
                    "id": "001", "title": "仮説A", "priority": "高", "status": "判定済み",
                    "verdict": "支持", "confidence": "中", "reason": "根拠A", "evidence": ["3件"],
                },
                {
                    "id": "002", "title": "仮説B", "priority": "中",
                    "status": "未検証(優先度中のため対象外)", "verdict": "未検証",
                    "confidence": "低", "reason": "対象外", "evidence": [],
                },
            ],
        },
        notifications.append,
        summarizer=summarize,
    ).execute(date(2026, 9, 1), date(2026, 9, 25))

    assert len(calls) == 1
    assert calls[0]["counts"]["untested_priority_excluded"] == 1
    assert "仮説A" in json.dumps(calls[0], ensure_ascii=False)
    assert result["summary_path"].read_text(encoding="utf-8").strip() == summary.strip()
    assert result["summary_path"].name == "strategy_review_summary.md"
    assert len(notifications) == 1
    assert "横断所見" in notifications[0]
    assert result["summary_path"].as_posix() in notifications[0].replace("\\", "/")
    assert len(notifications[0]) < len(summary)
    assert "処理時間:" in notifications[0]


def test_run_strategy_review_summary_failure_falls_back_without_losing_notification(tmp_path):
    notifications = []

    def fail_summary(payload):
        raise RuntimeError("summary unavailable")

    result = RunStrategyReviewUseCase(
        lambda start, end: {"output_path": tmp_path / "advisor.md"},
        lambda path: {
            "run_directory": tmp_path / "run",
            "feedback_path": tmp_path / "run" / "verified_hypotheses.json",
            "results": [{"status": "判定済み", "verdict": "棄却"}],
        },
        notifications.append,
        summarizer=fail_summary,
    ).execute(date(2026, 9, 1), date(2026, 9, 25))

    assert result["summary_failed"] is True
    assert result["review_summary"] is None
    assert result["summary_path"] is None
    assert len(notifications) == 1
    assert "検証完了" in notifications[0]
    assert "総括LLMは利用できませんでした" in notifications[0]


@pytest.mark.parametrize("failed_stage", ["advisor", "verifier"])
def test_run_strategy_review_stops_and_notifies_once_on_failure(tmp_path, failed_stage):
    notifications = []
    verification_calls = []
    advisor_path = tmp_path / "advisor.md"

    def advisor(start, end):
        if failed_stage == "advisor":
            raise RuntimeError("advisor failed")
        return {"output_path": advisor_path}

    def verifier(path):
        verification_calls.append(path)
        if failed_stage == "verifier":
            raise RuntimeError("verifier failed")
        return {"results": [], "run_directory": tmp_path, "feedback_path": tmp_path / "feedback.json"}

    use_case = RunStrategyReviewUseCase(advisor, verifier, notifications.append)

    with pytest.raises(RuntimeError):
        use_case.execute(date(2026, 9, 1), date(2026, 9, 25))

    assert len(notifications) == 1
    assert "失敗しました" in notifications[0]
    if failed_stage == "advisor":
        assert verification_calls == []
        assert "仮説生成" in notifications[0]
    else:
        assert verification_calls == [advisor_path]
        assert "仮説検証" in notifications[0]
        assert str(advisor_path) in notifications[0]


def test_run_strategy_review_rejects_invalid_period_without_calling_operations():
    calls = []
    use_case = RunStrategyReviewUseCase(
        lambda start, end: calls.append("advisor"),
        lambda path: calls.append("verifier"),
        lambda message: True,
    )

    with pytest.raises(ValueError):
        use_case.execute(date(2026, 9, 25), date(2026, 9, 1))

    assert calls == []


def test_review_entrypoint_reuses_daily_analyzer_or_disables_summary(monkeypatch):
    class Analyzer:
        def analyze_strategy_review(self, summary):
            return "横断レビュー"

    monkeypatch.setattr(run_strategy_review, "create_daily_analyzer", lambda: Analyzer())
    summarizer = run_strategy_review._build_summarizer()
    assert summarizer({"counts": {}}) == "横断レビュー"

    monkeypatch.setattr(run_strategy_review, "create_daily_analyzer", lambda: None)
    assert run_strategy_review._build_summarizer() is None