import json
from datetime import date

from src.application.generate_strategy_hypotheses_usecase import (
    GenerateStrategyHypothesesUseCase,
    build_strategy_hypothesis_prompt,
    estimate_prompt_tokens,
)
from src.infrastructure.analysis import strategy_hypothesis_analyzer
from src.infrastructure.analysis.strategy_hypothesis_context_repository import (
    StrategyHypothesisContextRepository,
    StrategyHypothesisReportRepository,
    extract_adr_summaries,
)
from src.infrastructure.notification import slack_notify


def test_extract_adr_summaries_supports_heading_and_inline_status_formats(tmp_path):
    adr_directory = tmp_path / "adr"
    adr_directory.mkdir()
    (adr_directory / "0001-heading.md").write_text(
        "# ADR-0001: 見出し形式\n\n## ステータス\n\n承認済み\n\n"
        "## 背景\n背景全文は含めない。\n\n## 決定\n\n"
        "本番相当サイジングをデフォルトにする。\n",
        encoding="utf-8",
    )
    (adr_directory / "0002-inline.md").write_text(
        "# ADR-0002: インライン形式\n\n- ステータス: Accepted\n\n"
        "## 決定\n\n既定動作を変更しない。\n",
        encoding="utf-8",
    )
    (adr_directory / "用途.md").write_text("# ADR記録の使い方\n", encoding="utf-8")

    summaries = extract_adr_summaries(adr_directory)

    assert len(summaries) == 2
    assert summaries[0] == {
        "title": "ADR-0001: 見出し形式",
        "status": "承認済み",
        "decision": "本番相当サイジングをデフォルトにする。",
    }
    assert "背景全文" not in json.dumps(summaries, ensure_ascii=False)
    assert summaries[1]["status"] == "Accepted"


def test_context_aggregates_matching_summary_and_optional_diagnostic_reports(tmp_path):
    weekly = tmp_path / "weekly"
    monthly = tmp_path / "monthly"
    analysis = tmp_path / "analysis"
    adr = tmp_path / "adr"
    for directory in (weekly, monthly, analysis, adr):
        directory.mkdir()
    (weekly / "2026-09-07_2026-09-11.json").write_text(
        json.dumps(
            {
                "period": {"start": "2026-09-07", "end": "2026-09-11", "status": "complete"},
                "daily": {"report_count": 5, "order_count": 3, "total_profit_loss": 10.0},
                "backtest": {
                    "exact_period_run_available": True,
                    "total_pnl": 25.5,
                    "total_trades": 8,
                    "runs": [{"total_pnl": 25.5}],
                },
                "llm_analysis": "過去LLM本文は除外する",
            }
        ),
        encoding="utf-8",
    )
    (analysis / "filter_entry_alignment_2026-09-07_2026-09-11.json").write_text(
        json.dumps({"start_date": "2026-09-07", "end_date": "2026-09-11", "counts": {"matched": 3}}),
        encoding="utf-8",
    )
    (adr / "0001.md").write_text(
        "# ADR-0001: 決定\n\n## ステータス\n\nAccepted\n\n## 決定\n\n現行仕様を維持する。\n",
        encoding="utf-8",
    )

    repository = StrategyHypothesisContextRepository(weekly, monthly, analysis, adr)
    context = repository.load_context(date(2026, 9, 7), date(2026, 9, 11))

    assert len(context["weekly_monthly_analyses"]) == 1
    assert context["paper_vs_backtest"]["available"] is True
    assert context["paper_vs_backtest"]["backtest_minus_paper_pnl"] == 15.5
    assert context["filter_entry_alignment"]["available"] is True
    assert context["regime_skip_severity"]["available"] is False
    assert any("レジームskip診断結果ファイルがありません" in item for item in context["data_warnings"])
    assert "llm_analysis" not in json.dumps(context, ensure_ascii=False)


def test_context_warns_when_persisted_analysis_and_diagnostics_are_missing(tmp_path):
    directories = [tmp_path / name for name in ("weekly", "monthly", "analysis", "adr")]
    for directory in directories:
        directory.mkdir()
    context = StrategyHypothesisContextRepository(*directories).load_context(
        date(2026, 9, 1), date(2026, 9, 5)
    )

    assert context["weekly_monthly_analyses"] == []
    assert len(context["data_warnings"]) == 3
    assert context["paper_vs_backtest"]["available"] is False


def test_context_includes_latest_verified_hypothesis_feedback(tmp_path):
    weekly, monthly, analysis, adr, verification = [
        tmp_path / name for name in ("weekly", "monthly", "analysis", "adr", "verification")
    ]
    for directory in (weekly, monthly, analysis, adr):
        directory.mkdir()
    old_run = verification / "20260920T100000_000000"
    latest_run = verification / "20260927T100000_000000"
    old_run.mkdir(parents=True)
    latest_run.mkdir()
    old_run.joinpath("verified_hypotheses.json").write_text(
        json.dumps({"hypotheses": [{"title": "既知仮説", "verdict": "支持", "reason": "古い"}]}),
        encoding="utf-8",
    )
    latest_run.joinpath("verified_hypotheses.json").write_text(
        json.dumps({
            "verified_at": "2026-09-27T10:00:00+09:00",
            "hypotheses": [
                {
                    "title": "既知仮説", "status": "判定済み", "verdict": "棄却",
                    "confidence": "高", "reason": "最新の定量結果", "evidence": ["0件"],
                }
            ],
        }),
        encoding="utf-8",
    )

    context = StrategyHypothesisContextRepository(
        weekly, monthly, analysis, adr, verification
    ).load_context(date(2026, 9, 1), date(2026, 9, 5))

    assert context["past_strategy_verifications"] == [
        {
            "title": "既知仮説",
            "status": "判定済み",
            "verdict": "棄却",
            "confidence": "高",
            "reason": "最新の定量結果",
            "evidence": ["0件"],
            "verified_at": "2026-09-27T10:00:00+09:00",
        }
    ]
    prompt = build_strategy_hypothesis_prompt(context)
    assert "棄却済み仮説は再提示しない" in prompt
    assert "不足していた新データが今回含まれる場合のみ再検討" in prompt
    assert "既知仮説" in prompt


def test_openai_analyzer_uses_fixed_system_prompt_and_existing_chat_completions_auth(monkeypatch):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "## 観察\n- 事実"}}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return DummyResponse()

    monkeypatch.setattr(strategy_hypothesis_analyzer.requests, "post", post)
    analyzer = strategy_hypothesis_analyzer.OpenAIStrategyHypothesisAnalyzer(
        "test-key", "test-model", "https://example.test"
    )

    result = analyzer.analyze("集約データ")

    assert result == "## 観察\n- 事実"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["messages"][0]["content"] == strategy_hypothesis_analyzer.SYSTEM_PROMPT
    assert captured["json"]["messages"][1]["content"] == "集約データ"


def test_use_case_saves_markdown_without_sending_slack_notification(tmp_path, monkeypatch):
    class ContextRepository:
        def load_context(self, start, end):
            return {"period": {"start": start.isoformat(), "end": end.isoformat()}}

    class Analyzer:
        def analyze(self, prompt):
            assert "## 優先度" in prompt
            assert '"period"' in prompt
            return "## 観察\n- 注文数0\n\n## 仮説\n### 仮説1: 条件未達\n- 根拠: 注文数0\n- 反証しうるデータ: シグナルログ\n- 検証方法(追加分析案): ログ確認\n\n## 優先度\n- 中: 追加観測が必要"

    sent_requests = []
    monkeypatch.setattr(
        slack_notify.requests,
        "post",
        lambda *args, **kwargs: sent_requests.append((args, kwargs)),
    )
    use_case = GenerateStrategyHypothesesUseCase(
        ContextRepository(),
        Analyzer(),
        StrategyHypothesisReportRepository(tmp_path),
    )

    result = use_case.execute(date(2026, 9, 7), date(2026, 9, 11))

    assert result["output_path"].name == "2026-09-07_2026-09-11.md"
    assert result["output_path"].read_text(encoding="utf-8").startswith("# 戦略仮説アドバイザー")
    assert sent_requests == []
    assert result["estimated_input_tokens"] == estimate_prompt_tokens(
        strategy_hypothesis_analyzer.SYSTEM_PROMPT,
        build_strategy_hypothesis_prompt(result["context"]),
    )