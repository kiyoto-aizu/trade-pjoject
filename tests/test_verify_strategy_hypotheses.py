import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.analysis import verify_strategy_hypotheses as verify_entrypoint
from src.application.verify_strategy_hypotheses_usecase import (
    CODE_GENERATION_SYSTEM_PROMPT,
    StrategyHypothesis,
    VerifyStrategyHypothesesUseCase,
    parse_hypothesis_report,
    parse_judgment,
    parse_research_plan,
)
from src.infrastructure.analysis.strategy_analysis_code_sandbox import StrategyAnalysisExecutionResult
from src.infrastructure.analysis import strategy_verification_client
from src.infrastructure.analysis.strategy_analysis_code_sandbox import validate_generated_code
from src.infrastructure.analysis.strategy_external_market_data_client import StrategyExternalMarketDataClient


def test_parse_hypothesis_report_extracts_period_titles_and_verification_methods():
    report = """# 戦略仮説アドバイザー

対象期間: 2026-09-21 ～ 2026-09-25

## 仮説
### 仮説1: フィルターが過度に遮断
- 根拠: skipイベントが多い
- 反証しうるデータ: 候補母集団
- 検証方法(追加分析案):
  - event_typeごとのoutcome件数を集計する
  - 対象期間のイベントだけを見る

### 仮説2: market assessmentが未評価
- 根拠: not_evaluatedが2日
- 検証方法(追加分析案): 状態別の件数を集計

## 優先度
- 高: 仮説1
- 中: 仮説2
"""

    start, end, hypotheses = parse_hypothesis_report(report)

    assert (start, end) == (date(2026, 9, 21), date(2026, 9, 25))
    assert hypotheses == [
        StrategyHypothesis(
            "フィルターが過度に遮断",
            "event_typeごとのoutcome件数を集計する\n対象期間のイベントだけを見る",
            "1",
            "高",
        ),
        StrategyHypothesis("market assessmentが未評価", "状態別の件数を集計", "2", "中"),
    ]


def test_priority_parser_supports_legacy_bullets_without_hypothesis_ids():
    report = """対象期間: 2026-09-07 ～ 2026-09-11
### 仮説1: 候補がゼロ
- 検証方法(追加分析案): 候補数を集計
### 仮説2: 未清算
- 検証方法(追加分析案): 終端ポジション数を集計
### 仮説3: ログ不足
- 検証方法(追加分析案): ログの有無を確認
### 仮説4: 診断ファイル欠落
- 検証方法(追加分析案): ファイル一覧を確認

## 優先度
- 高。zero-trade runを確認する
---
- 中。終端ポジションを確認する
---
- 高。突合可能性を確認する
---
- 中。診断欠落を確認する
"""

    _, _, hypotheses = parse_hypothesis_report(report)

    assert [hypothesis.priority for hypothesis in hypotheses] == ["高", "中", "高", "中"]


def test_verifier_saves_code_execution_outputs_judgment_and_feedback(tmp_path):
    report_path = tmp_path / "advisor.md"
    report_path.write_text(
        "# 戦略仮説アドバイザー\n\n対象期間: 2026-09-21 ～ 2026-09-25\n\n"
        "### 仮説1: スキップ結果に偏りがある\n"
        "- 検証方法(追加分析案): event_typeごとのoutcome件数を集計\n\n"
        "## 優先度\n- 高: 仮説1\n",
        encoding="utf-8",
    )

    class Client:
        def __init__(self):
            self.responses = iter([
                json.dumps({
                    "needs_internal_data": True,
                    "external_indices": [],
                    "external_news_needed": False,
                    "questions": ["イベント種別ごとの件数"],
                    "missing_sources": [],
                }),
                "```python\nprint(query_filter_events(\"SELECT event_type, COUNT(*) AS n FROM filter_decision_events GROUP BY event_type\"))\n```",
                json.dumps({
                    "verdict": "支持",
                    "confidence": "中",
                    "reason": "観測されたイベントに該当結果がある",
                    "evidence": ["ATR_DANGER_SKIP: 3件"],
                }, ensure_ascii=False),
            ])
            self.prompts = []

        def complete(self, system_prompt, user_prompt):
            self.prompts.append(user_prompt)
            return next(self.responses)

    class Sandbox:
        timeout_seconds = 60

        def describe_data_sources(self, start, end):
            return {
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "parquet": {"columns": [], "sha256_by_file": [{"sha256": "audit-only"}]},
                "sqlite": {"columns": [], "sha256_files": {"db": "audit-only"}},
            }

        def compare_source_fingerprints(self, schema):
            return {"matches": True, "mismatches": []}

        def run(self, code, start, end):
            assert "query_filter_events" in code
            return StrategyAnalysisExecutionResult(
                stdout="[{'event_type': 'ATR_DANGER_SKIP', 'n': 3}]\n",
                stderr="",
                elapsed_seconds=0.1,
                timed_out=False,
                return_code=0,
                status="succeeded",
            )

    client = Client()
    result = VerifyStrategyHypothesesUseCase(
        client, Sandbox(), tmp_path / "verification"
    ).execute(report_path)
    run_directory = result["run_directory"]
    hypothesis_directory = run_directory / "hypotheses" / "hypothesis_001"
    feedback = json.loads(result["feedback_path"].read_text(encoding="utf-8"))

    assert (run_directory / "advisor_report.md").exists()
    assert (hypothesis_directory / "analysis_code.py").read_text(encoding="utf-8").startswith("print(")
    assert (hypothesis_directory / "code_generation_system_prompt.txt").read_text(encoding="utf-8").startswith("あなたは")
    assert (hypothesis_directory / "judgment_system_prompt.txt").read_text(encoding="utf-8").startswith("あなたは")
    assert (hypothesis_directory / "execution_stdout.txt").read_text(encoding="utf-8").startswith("[")
    assert json.loads((hypothesis_directory / "execution_metadata.json").read_text(encoding="utf-8"))["elapsed_seconds"] == 0.1
    assert json.loads((hypothesis_directory / "raw_data_for_judgment.json").read_text(encoding="utf-8"))["stdout"]
    assert feedback["hypotheses"][0]["verdict"] == "支持"
    assert "判定: 支持" in (run_directory / "results.md").read_text(encoding="utf-8")
    assert "audit-only" not in client.prompts[0]

    replay = VerifyStrategyHypothesesUseCase(
        None, Sandbox(), tmp_path / "unused"
    ).replay_saved_code(hypothesis_directory / "analysis_code.py")

    assert replay["execution"].status == "succeeded"
    assert replay["source_fingerprints"]["matches"] is True
    assert (replay["replay_directory"] / "analysis_code.py").exists()


def test_pipeline_executes_only_high_priority_and_records_other_priorities_as_untested(tmp_path):
    report_path = tmp_path / "advisor.md"
    report_path.write_text(
        "対象期間: 2026-09-21 ～ 2026-09-25\n"
        "### 仮説1: 高優先度仮説\n- 検証方法(追加分析案): 高だけ調査\n"
        "### 仮説2: 中優先度仮説\n- 検証方法(追加分析案): 中は手動選択待ち\n"
        "### 仮説3: 低優先度仮説\n- 検証方法(追加分析案): 低は手動選択待ち\n\n"
        "## 優先度\n- 高: 仮説1\n- 中: 仮説2\n- 低: 仮説3\n",
        encoding="utf-8",
    )

    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, system_prompt, user_prompt):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({
                    "needs_internal_data": True,
                    "external_indices": [],
                    "external_news_needed": False,
                    "questions": ["高優先度の件数"],
                    "missing_sources": [],
                })
            if self.calls == 2:
                return 'print(query_filter_events("SELECT COUNT(*) AS n FROM filter_decision_events"))'
            return json.dumps({
                "verdict": "支持", "confidence": "中",
                "reason": "対象件数を確認した", "evidence": ["1件"],
            }, ensure_ascii=False)

    class Sandbox:
        timeout_seconds = 60

        def describe_data_sources(self, start, end):
            return {}

        def run(self, code, start, end):
            return StrategyAnalysisExecutionResult("[{'n': 1}]", "", 0.1, False, 0, "succeeded")

    client = Client()
    result = VerifyStrategyHypothesesUseCase(
        client, Sandbox(), tmp_path / "verification"
    ).execute(report_path)
    feedback = json.loads(result["feedback_path"].read_text(encoding="utf-8"))

    assert client.calls == 3
    assert [item["status"] for item in result["results"]] == [
        "判定済み",
        "未検証(優先度中のため対象外)",
        "未検証(優先度低のため対象外)",
    ]
    assert result["results"][1]["verdict"] == "未検証"
    assert feedback["hypotheses"][1]["priority"] == "中"
    assert json.loads(
        (result["run_directory"] / "hypotheses" / "hypothesis_002" / "skipped.json").read_text(encoding="utf-8")
    )["status"] == "未検証(優先度中のため対象外)"


def test_verification_continues_after_one_hypothesis_generation_error(tmp_path):
    report_path = tmp_path / "advisor.md"
    report_path.write_text(
        "対象期間: 2026-09-21 ～ 2026-09-25\n"
        "### 仮説1: 先行仮説\n- 検証方法(追加分析案): 集計する\n"
        "### 仮説2: 後続仮説\n- 検証方法(追加分析案): 別の集計をする\n\n"
        "## 優先度\n- 高: 仮説1\n- 高: 仮説2\n",
        encoding="utf-8",
    )

    class Client:
        def __init__(self):
            self.calls = 0

        def complete(self, system_prompt, user_prompt):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("mock generation failure")
            if self.calls == 2:
                return json.dumps({
                    "needs_internal_data": True,
                    "external_indices": [],
                    "external_news_needed": False,
                    "questions": ["イベント件数"],
                    "missing_sources": [],
                })
            if self.calls == 3:
                return 'print(query_filter_events("SELECT COUNT(*) AS n FROM filter_decision_events"))'
            return json.dumps({
                "verdict": "追加データ必要",
                "confidence": "低",
                "reason": "対象行が不足",
                "evidence": [],
            }, ensure_ascii=False)

    class Sandbox:
        timeout_seconds = 60

        def describe_data_sources(self, start, end):
            return {}

        def run(self, code, start, end):
            return StrategyAnalysisExecutionResult("[{'n': 0}]", "", 0.1, False, 0, "succeeded")

    results = VerifyStrategyHypothesesUseCase(
        Client(), Sandbox(), tmp_path / "verification"
    ).execute(report_path)["results"]

    assert len(results) == 2
    assert results[0]["status"] == "検証不能"
    assert results[0]["verdict"] == "追加データ必要"
    assert results[1]["status"] == "判定済み"


def test_judgment_parser_rejects_invalid_verdict_and_confidence():
    assert parse_judgment(
        '{"verdict":"棄却","confidence":"高","reason":"値が0","evidence":["0件"]}'
    )["verdict"] == "棄却"
    with pytest.raises(ValueError):
        parse_judgment(
            '{"verdict":"パラメータ変更","confidence":"高","reason":"変更する","evidence":[]}'
        )


def test_verification_client_uses_existing_bearer_auth_and_chat_completions_payload(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "print(1)"}}]}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(strategy_verification_client.requests, "post", post)
    client = strategy_verification_client.OpenAIStrategyVerificationClient(
        "test-key", "test-model", "https://example.test"
    )

    assert client.complete("system", "user") == "print(1)"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "test-model"
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured["timeout"] == 90.0


def test_code_generation_prompt_example_matches_sandbox_literal_sql_contract():
    assert "SQLを別変数へ代入" in CODE_GENERATION_SYSTEM_PROMPT
    assert "関数定義(def/lambda)" in CODE_GENERATION_SYSTEM_PROMPT
    assert "SQLite式のSTRFTIME(format_string, timestamp)は禁止" in CODE_GENERATION_SYSTEM_PROMPT
    validate_generated_code(
        'rows = query_filter_events("SELECT event_type, COUNT(*) AS n FROM filter_decision_events GROUP BY event_type")\n'
        "print(rows)"
    )


def test_research_plan_parser_limits_external_symbols():
    plan = parse_research_plan(
        '{"needs_internal_data":false,"external_indices":["^N225","^VIX"],'
        '"external_news_needed":true,"questions":["指数変化"],"missing_sources":["ニュース"]}'
    )

    assert plan["external_indices"] == ["^N225", "^VIX"]
    assert plan["external_news_needed"] is True
    with pytest.raises(ValueError):
        parse_research_plan(
            '{"needs_internal_data":false,"external_indices":["^UNKNOWN"],'
            '"questions":[],"missing_sources":[]}'
        )


def test_deep_dive_includes_previous_missing_data_and_selects_only_requested_hypothesis(tmp_path):
    report_path = tmp_path / "advisor.md"
    report_path.write_text(
        "対象期間: 2026-09-21 ～ 2026-09-25\n"
        "### 仮説1: 別仮説\n- 検証方法(追加分析案): 別分析\n"
        "### 仮説2: 不足データの再調査\n- 検証方法(追加分析案): 注文記録を追加で確認\n",
        encoding="utf-8",
    )
    output_directory = tmp_path / "verification"
    previous_run = output_directory / "20260926T100000_000000"
    previous_hypothesis = previous_run / "hypotheses" / "hypothesis_002"
    previous_hypothesis.mkdir(parents=True)
    (previous_run / "verified_hypotheses.json").write_text(
        json.dumps({
            "verified_at": "2026-09-26T10:00:00+09:00",
            "source_report": "old.md",
            "period": {"start": "2026-09-14", "end": "2026-09-18"},
            "hypotheses": [{
                "id": "002", "title": "不足データの再調査", "status": "判定済み",
                "verdict": "追加データ必要", "confidence": "低",
                "reason": "前回は注文記録データがなかった",
                "evidence": ["イベント表だけを調査"],
            }],
        }),
        encoding="utf-8",
    )
    (previous_hypothesis / "raw_data_for_judgment.json").write_text(
        json.dumps({"stdout": "前回はイベント集計のみ", "stderr": "", "status": "succeeded"}),
        encoding="utf-8",
    )

    class Client:
        def __init__(self):
            self.prompts = []
            self.responses = iter([
                json.dumps({
                    "needs_internal_data": False,
                    "external_indices": [],
                    "external_news_needed": True,
                    "questions": ["注文記録の追加データ"],
                    "missing_sources": ["注文履歴"],
                }, ensure_ascii=False),
                json.dumps({
                    "verdict": "追加データ必要", "confidence": "低",
                    "reason": "注文履歴がない", "evidence": [],
                }, ensure_ascii=False),
            ])

        def complete(self, system_prompt, user_prompt):
            self.prompts.append(user_prompt)
            return next(self.responses)

    class Sandbox:
        timeout_seconds = 60

        def describe_data_sources(self, start, end):
            return {"sqlite": {"columns": [], "sha256_files": {"db": "secret"}}}

    client = Client()
    result = VerifyStrategyHypothesesUseCase(
        client, Sandbox(), output_directory
    ).execute_selected(report_path, "2")

    assert len(result["results"]) == 1
    assert result["results"][0]["id"] == "002"
    assert result["results"][0]["status"] == "判定済み"
    assert "前回は注文記録データがなかった" in client.prompts[0]
    assert "前回はイベント集計のみ" in client.prompts[0]
    assert "secret" not in "".join(client.prompts)
    assert result["run_directory"].joinpath("advisor_report.md").exists()
    assert json.loads((result["run_directory"] / "run_metadata.json").read_text(encoding="utf-8"))["mode"] == "deep_dive"


def test_adhoc_uses_external_index_without_advisor_report_or_internal_code(tmp_path):
    class Client:
        def __init__(self):
            self.responses = iter([
                json.dumps({
                    "needs_internal_data": False,
                    "external_indices": ["^N225"],
                    "external_news_needed": True,
                    "questions": ["指数変化"],
                    "missing_sources": [],
                }, ensure_ascii=False),
                json.dumps({
                    "verdict": "追加データ必要", "confidence": "低",
                    "reason": "指数だけでは個別銘柄の原因を特定できない", "evidence": ["N225 close"],
                }, ensure_ascii=False),
            ])

        def complete(self, system_prompt, user_prompt):
            return next(self.responses)

    class Sandbox:
        timeout_seconds = 60

        def describe_data_sources(self, start, end):
            return {}

        def run(self, code, start, end):
            raise AssertionError("internal analysis was not requested")

    class ExternalData:
        def load_index_data(self, symbols, start, end):
            return {
                "provider": "Yahoo Finance Chart API",
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "results": [{
                    "symbol": "^N225", "url": "https://query1.finance.yahoo.com/example",
                    "status": "available", "rows": [{"date": "2026-09-21", "close": 45000.0}],
                }],
            }

    result = VerifyStrategyHypothesesUseCase(
        Client(), Sandbox(), tmp_path / "verification", ExternalData()
    ).execute_adhoc("市場全体の下落が見送りに影響したか", date(2026, 9, 21), date(2026, 9, 25))
    run_directory = result["run_directory"]
    hypothesis_directory = run_directory / "hypotheses" / "hypothesis_001"

    assert result["results"][0]["status"] == "判定済み"
    assert (run_directory / "adhoc_hypothesis.txt").exists()
    assert not (run_directory / "advisor_report.md").exists()
    external = json.loads((hypothesis_directory / "external_data.json").read_text(encoding="utf-8"))
    assert external["results"][0]["url"].startswith("https://")
    assert external["external_news"]["requested"] is True
    assert external["external_news"]["available"] is False
    assert not (hypothesis_directory / "analysis_code.py").read_text(encoding="utf-8").strip()


def test_external_market_client_filters_period_and_records_source_url():
    class IndexClient:
        SUPPORTED_SYMBOLS = {"^N225"}

        def get_daily_ohlc(self, symbol, range_):
            return [
                SimpleNamespace(date=date(2026, 9, 21), open=1, high=2, low=0, close=1.5),
                SimpleNamespace(date=date(2026, 9, 26), open=2, high=3, low=1, close=2.5),
            ]

    result = StrategyExternalMarketDataClient(IndexClient()).load_index_data(
        ["^N225", "^BAD"], date(2026, 9, 21), date(2026, 9, 25)
    )

    assert result["results"][0]["rows"] == [
        {"date": "2026-09-21", "open": 1, "high": 2, "low": 0, "close": 1.5}
    ]
    assert result["results"][0]["url"] == "https://query1.finance.yahoo.com/v8/finance/chart/^N225"
    assert result["results"][1]["status"] == "unsupported_symbol"


def test_verifier_cli_accepts_all_three_input_modes():
    parser = verify_entrypoint._build_parser()

    assert parser.parse_args(["advisor.md"]).advisor_report == Path("advisor.md")
    deep_dive = parser.parse_args(["--hypothesis-file", "advisor.md", "--hypothesis-id", "2"])
    assert deep_dive.hypothesis_id == "2"
    assert parser.parse_args(["--adhoc", "指数下落と見送りの関係"]).adhoc == "指数下落と見送りの関係"


def test_cli_pipeline_mode_does_not_send_standalone_slack_notification(monkeypatch, tmp_path):
    report = tmp_path / "advisor.md"
    report.write_text("placeholder", encoding="utf-8")
    notifications = []

    class FakeUseCase:
        def __init__(self, *args):
            pass

        def execute(self, input_report):
            return {
                "markdown": "pipeline result",
                "run_directory": tmp_path / "run",
                "feedback_path": tmp_path / "run" / "verified_hypotheses.json",
            }

    monkeypatch.setattr(verify_entrypoint.sys, "argv", ["verify", str(report)])
    monkeypatch.setattr(verify_entrypoint, "create_strategy_verification_client", lambda: object())
    monkeypatch.setattr(verify_entrypoint, "StrategyAnalysisCodeSandbox", lambda **kwargs: object())
    monkeypatch.setattr(verify_entrypoint, "VerifyStrategyHypothesesUseCase", FakeUseCase)
    monkeypatch.setattr(verify_entrypoint, "notify_analysis", notifications.append)

    verify_entrypoint.main()

    assert notifications == []


@pytest.mark.parametrize(
    ("arguments", "expected_notifications"),
    [
        (["advisor.md"], 0),
        (["--hypothesis-file", "advisor.md", "--hypothesis-id", "2"], 1),
        (["--adhoc", "市況と見送りの関係", "--start-date", "2026-09-21", "--end-date", "2026-09-25"], 1),
    ],
)
def test_cli_modes_notify_only_for_deep_dive_and_adhoc(
    monkeypatch, tmp_path, arguments, expected_notifications
):
    report = tmp_path / "advisor.md"
    report.write_text("placeholder", encoding="utf-8")
    notifications = []

    class FakeUseCase:
        def __init__(self, *args):
            pass

        def _result(self):
            return {
                "markdown": "result",
                "run_directory": tmp_path / "run",
                "feedback_path": tmp_path / "run" / "verified_hypotheses.json",
                "results": [{
                    "title": "調査仮説", "verdict": "支持", "confidence": "中", "reason": "件数を確認",
                }],
            }

        def execute(self, path):
            return self._result()

        def execute_selected(self, path, hypothesis_id):
            return self._result()

        def execute_adhoc(self, text, start, end):
            return self._result()

    cli_arguments = [str(report) if value == "advisor.md" else value for value in arguments]
    monkeypatch.setattr(verify_entrypoint.sys, "argv", ["verify", *cli_arguments])
    monkeypatch.setattr(verify_entrypoint, "create_strategy_verification_client", lambda: object())
    monkeypatch.setattr(verify_entrypoint, "StrategyAnalysisCodeSandbox", lambda **kwargs: object())
    monkeypatch.setattr(verify_entrypoint, "VerifyStrategyHypothesesUseCase", FakeUseCase)
    monkeypatch.setattr(verify_entrypoint, "notify_analysis", lambda message: notifications.append(message) or True)

    verify_entrypoint.main()

    assert len(notifications) == expected_notifications