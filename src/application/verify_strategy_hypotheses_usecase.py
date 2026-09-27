from __future__ import annotations

import json
import logging
import platform
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.infrastructure.analysis.strategy_analysis_code_sandbox import (
    DUCKDB_MEMORY_LIMIT,
    MAX_AST_NODES,
    MAX_CODE_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_QUERY_ROWS,
    StrategyAnalysisCodeSandbox,
    StrategyAnalysisExecutionResult,
)
from src.infrastructure.analysis.strategy_external_market_data_client import (
    StrategyExternalMarketDataClient,
)
from src.infrastructure.analysis.strategy_verification_client import OpenAIStrategyVerificationClient
import duckdb
import sqlglot

logger = logging.getLogger(__name__)

CODE_GENERATION_SYSTEM_PROMPT = """あなたは日本株自動売買システムのデータ分析コードを書くエンジニアです。
与えられた仮説の検証方法に基づき、実データに対する読み取り専用のPython分析コードを1つ生成してください。
以下を厳守すること:
1. データストアへの書き込みは一切行わない
2. 与えられたスキーマ情報にないカラム名・テーブル名を使わない
3. 結果は必ず標準出力にprintし、後続処理が読み取れる形にする
4. 実行時間が長くなりそうな全件スキャンは避け、対象期間のデータに絞る
5. import、ファイル操作、ネットワーク、サブプロセス、特殊属性アクセスは使わない。取得した行dictへの.get(key)だけ許可
6. 利用できる関数はquery_parquet(sql)、query_filter_events(sql)、print、len、sum、min、max、sorted、round、abs、float、int、strのみ
7. query関数へSQLを渡すときは、SQL全文を関数呼び出しの直接の文字列リテラルにする。SQLを別変数へ代入したり、連結・書式展開で組み立てたりしない
8. SQLではCOUNT/SUM/AVG/MIN/MAX/ROUND/ABS/CAST/COALESCE/NULLIF/GREATEST/LEAST/LOWER/UPPER/SUBSTR/SUBSTRING/STRFTIME/TIME_TO_STR/DATE_PART/DATE_TRUNCのみ使用できる
9. Parquetはminute_bars、SQLiteはfilter_decision_eventsだけを参照し、各クエリは単一のSELECT文にする
10. 1クエリ最大1000行。集計はSQL側で行う
11. SQL変数、関数定義(def/lambda)、クラス定義、再帰、複数クエリ結果のPython内結合は行わない
12. クエリ結果に対する単一レベルのリスト内包表記/ジェネレーター式、最大8回の有限for、数値の加減乗除と±2以内の累乗を許可する。ネスト内包表記は禁止
13. 空または100件以下のローカルリストへの.append(value)だけ許可。その他のメソッド呼び出しは禁止
14. コードは最大2つの独立したクエリ呼び出しを使い、必要十分な集計にとどめる
15. SQL方言はDuckDB。日付文字列の絞り込みはSUBSTR(occurred_at, 1, 10)を優先する
16. STRFTIMEを使う場合はDuckDBの順序 STRFTIME(timestamp_expression, format_string) にする。SQLite式のSTRFTIME(format_string, timestamp)は禁止

許可される形式の例:
rows = query_filter_events("SELECT event_type, COUNT(*) AS event_count FROM filter_decision_events GROUP BY event_type")
print(rows)

Parquetの場合:
rows = query_parquet("SELECT symbol, COUNT(*) AS bar_count FROM minute_bars GROUP BY symbol")
print(rows)

期間で絞る場合:
rows = query_filter_events("SELECT event_type, COUNT(*) AS event_count FROM filter_decision_events WHERE SUBSTR(occurred_at, 1, 10) BETWEEN '2026-09-14' AND '2026-09-18' GROUP BY event_type")
print(rows)

Pythonコードのみを返し、Markdownコードフェンスや説明文を付けないこと。"""

RESEARCH_PLAN_SYSTEM_PROMPT = """あなたは日本株自動売買システムの仮説検証計画担当です。
仮説と検証方法を読み、結論に必要な調査方法を選択してください。
社内データの単純な集計で十分なら内部データだけを選び、不要な統計分析や外部情報を求めないでください。
市場全体の外部要因が不可欠な場合だけYahoo Financeの指数日足を選べます。
一般ニュース記事の検索は利用できません。ニュース本文が不可欠ならその不足を明示してください。
JSONオブジェクトのみを返してください。形式:
{"needs_internal_data":true,"external_indices":["^N225"],"external_news_needed":false,"questions":["確認する数値"],"missing_sources":[]} 
external_indicesで指定できるのは ^N225, ^VIX, ^DJI, ^GSPC のみです。"""

JUDGMENT_SYSTEM_PROMPT = """あなたは日本株自動売買システムの「仮説検証役」です。
内部データ分析結果または監査可能な外部指数データをもとに、仮説が支持されるか棄却されるかを判定してください。
以下を厳守すること:
1. 判定は「支持」「棄却」「追加データ必要」の3択とする
2. 判定の根拠は実際に取得した調査結果に限定する。取得していない情報を根拠にしない
3. 実行結果が仮説を判断するには不十分な場合は、正直に「追加データ必要」とする
4. 信頼度(高/中/低)を必ず付ける
JSONオブジェクトのみを返す。形式: {"verdict":"支持|棄却|追加データ必要","confidence":"高|中|低","reason":"数値に基づく理由","evidence":["実行結果の数値"]}"""

_PERIOD_PATTERN = re.compile(
    r"対象期間\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s*(?:～|〜|~|-)\s*(\d{4}-\d{2}-\d{2})"
)
_HYPOTHESIS_PATTERN = re.compile(r"^###\s*仮説\s*(\d+)\s*[:：]\s*(.+?)\s*$")
_PRIORITY_PATTERN = re.compile(r"^\s*[-*]\s*(高|中|低)\s*(?:[:：。.]\s*)?(.*)$")
_HYPOTHESIS_REFERENCE_PATTERN = re.compile(r"仮説\s*(\d+)")
_METHOD_PATTERN = re.compile(r"^\s*[-*]\s*検証方法\s*\(追加分析案\)\s*[:：]\s*(.*)$")
_SECTION_FIELD_PATTERN = re.compile(r"^\s*[-*]\s*(?:根拠|反証しうるデータ)\s*[:：]")
_VERDICTS = {"支持", "棄却", "追加データ必要"}
_CONFIDENCE = {"高", "中", "低"}
_SUPPORTED_EXTERNAL_INDICES = {"^N225", "^VIX", "^DJI", "^GSPC"}


@dataclass(frozen=True)
class StrategyHypothesis:
    title: str
    verification_method: str
    source_id: str = ""
    priority: str = "不明"


def parse_hypothesis_report(report_text: str) -> tuple[date, date, list[StrategyHypothesis]]:
    period_match = _PERIOD_PATTERN.search(report_text)
    if period_match is None:
        raise ValueError("入力レポートに「対象期間: YYYY-MM-DD ～ YYYY-MM-DD」がありません")
    start, end = (date.fromisoformat(value) for value in period_match.groups())
    if start > end:
        raise ValueError("入力レポートの対象期間が不正です")

    lines = report_text.splitlines()
    hypotheses: list[StrategyHypothesis] = []
    index = 0
    while index < len(lines):
        match = _HYPOTHESIS_PATTERN.match(lines[index].strip())
        if match is None:
            index += 1
            continue
        source_id = match.group(1)
        title = match.group(2).strip()
        index += 1
        method_lines: list[str] = []
        is_method = False
        while index < len(lines) and not lines[index].lstrip().startswith("### "):
            line = lines[index]
            if line.startswith("## "):
                break
            method_match = _METHOD_PATTERN.match(line)
            if method_match:
                is_method = True
                if method_match.group(1).strip():
                    method_lines.append(method_match.group(1).strip())
            elif is_method and _SECTION_FIELD_PATTERN.match(line):
                break
            elif is_method and line.strip():
                method_lines.append(re.sub(r"^\s*[-*]\s*", "", line).strip())
            index += 1
        hypotheses.append(
            StrategyHypothesis(
                title=title,
                verification_method="\n".join(method_lines).strip(),
                source_id=source_id,
            )
        )
    if not hypotheses:
        raise ValueError("入力レポートに「### 仮説N: タイトル」形式の仮説がありません")
    priority_by_id: dict[str, str] = {}
    priority_items: list[tuple[str, str, list[str]]] = []
    in_priority_section = False
    for line in lines:
        if line.strip().startswith("## "):
            in_priority_section = line.strip() == "## 優先度"
            continue
        if not in_priority_section:
            continue
        priority_match = _PRIORITY_PATTERN.match(line)
        if priority_match is None:
            continue
        priority, description = priority_match.groups()
        referenced_ids = _HYPOTHESIS_REFERENCE_PATTERN.findall(description)
        priority_items.append((priority, description, referenced_ids))
        for hypothesis in hypotheses:
            if hypothesis.source_id in referenced_ids or hypothesis.title in description:
                priority_by_id[hypothesis.source_id] = priority
    if (
        len(priority_items) == len(hypotheses)
        and all(not referenced_ids for _, _, referenced_ids in priority_items)
    ):
        for hypothesis, (priority, _, _) in zip(hypotheses, priority_items):
            priority_by_id.setdefault(hypothesis.source_id, priority)
    hypotheses = [
        StrategyHypothesis(
            title=hypothesis.title,
            verification_method=hypothesis.verification_method,
            source_id=hypothesis.source_id,
            priority=priority_by_id.get(hypothesis.source_id, "不明"),
        )
        for hypothesis in hypotheses
    ]
    return start, end, hypotheses


def extract_python_code(response: str) -> str:
    fenced = re.search(r"```(?:python|py)?\s*\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else response).strip()


def parse_judgment(response: str) -> dict[str, Any]:
    candidate = response.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    result = json.loads(candidate)
    if not isinstance(result, dict):
        raise ValueError("判定結果がJSONオブジェクトではありません")
    verdict = result.get("verdict")
    confidence = result.get("confidence")
    reason = result.get("reason")
    evidence = result.get("evidence")
    if verdict not in _VERDICTS or confidence not in _CONFIDENCE:
        raise ValueError("判定または信頼度が許可値ではありません")
    if not isinstance(reason, str) or not isinstance(evidence, list):
        raise ValueError("判定結果にreason/evidenceがありません")
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reason": reason,
        "evidence": [str(item) for item in evidence],
    }


def parse_research_plan(response: str) -> dict[str, Any]:
    candidate = response.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    plan = json.loads(candidate)
    if not isinstance(plan, dict) or not isinstance(plan.get("needs_internal_data"), bool):
        raise ValueError("調査計画にneeds_internal_dataの真偽値がありません")
    external_indices = plan.get("external_indices", [])
    questions = plan.get("questions", [])
    missing_sources = plan.get("missing_sources", [])
    if not isinstance(external_indices, list) or any(
        symbol not in _SUPPORTED_EXTERNAL_INDICES for symbol in external_indices
    ):
        raise ValueError("調査計画に未対応の外部指数が含まれています")
    if not all(isinstance(items, list) and all(isinstance(item, str) for item in items)
               for items in (questions, missing_sources)):
        raise ValueError("調査計画のquestions/missing_sources形式が不正です")
    external_news_needed = plan.get("external_news_needed", False)
    if not isinstance(external_news_needed, bool):
        raise ValueError("external_news_neededは真偽値で指定してください")
    return {
        "needs_internal_data": plan["needs_internal_data"],
        "external_indices": list(dict.fromkeys(external_indices)),
        "external_news_needed": external_news_needed,
        "questions": questions[:10],
        "missing_sources": missing_sources[:10],
    }


def _format_datetime(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


class VerifyStrategyHypothesesUseCase:
    def __init__(
        self,
        client: OpenAIStrategyVerificationClient | None,
        sandbox: StrategyAnalysisCodeSandbox,
        output_directory: Path,
        external_data_client: StrategyExternalMarketDataClient | None = None,
    ):
        self.client = client
        self.sandbox = sandbox
        self.output_directory = Path(output_directory)
        self.external_data_client = external_data_client or StrategyExternalMarketDataClient()

    def _load_previous_investigation(self, title: str) -> dict[str, Any] | None:
        candidates = sorted(
            self.output_directory.glob("*/verified_hypotheses.json"), reverse=True
        )
        for feedback_path in candidates:
            try:
                feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            for item in feedback.get("hypotheses", []):
                if not isinstance(item, dict) or item.get("title") != title:
                    continue
                if item.get("verdict") != "追加データ必要" and item.get("status") != "検証不能":
                    continue
                hypothesis_id = str(item.get("id", "")).zfill(3)
                hypothesis_directory = feedback_path.parent / "hypotheses" / f"hypothesis_{hypothesis_id}"
                raw_path = hypothesis_directory / "raw_data_for_judgment.json"
                try:
                    raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    raw_data = {}
                raw_data = {
                    "status": raw_data.get("status"),
                    "return_code": raw_data.get("return_code"),
                    "timed_out": raw_data.get("timed_out"),
                    "elapsed_seconds": raw_data.get("elapsed_seconds"),
                    "stdout": str(raw_data.get("stdout") or "")[:5000],
                    "stderr": str(raw_data.get("stderr") or "")[:1500],
                }
                return {
                    "verified_at": feedback.get("verified_at"),
                    "source_report": feedback.get("source_report"),
                    "period": feedback.get("period"),
                    "previous_status": item.get("status"),
                    "previous_verdict": item.get("verdict"),
                    "previous_reason": item.get("reason", ""),
                    "previous_evidence": item.get("evidence", [])[:5],
                    "previous_research_output": raw_data,
                }
        return None

    @staticmethod
    def _research_plan_prompt(
        hypothesis: StrategyHypothesis,
        start: date,
        end: date,
        schema: dict[str, Any],
        previous_investigation: dict[str, Any] | None,
    ) -> str:
        plan_context = {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "hypothesis": hypothesis.title,
            "verification_method": hypothesis.verification_method,
            "data_schema": VerifyStrategyHypothesesUseCase._schema_for_prompt(schema),
            "available_external_indices": sorted(_SUPPORTED_EXTERNAL_INDICES),
            "previous_investigation": previous_investigation,
        }
        instruction = (
            "前回が追加データ必要の場合は、既に実施した調査を繰り返さず、不足点を補う情報源・分析を選んでください。"
            if previous_investigation
            else "仮説の性質に合う最小限の調査だけを選んでください。"
        )
        return (
            f"{instruction}\n"
            "内部SQLite/ParquetとYahoo指数のどれが必要かを判定し、指定JSON形式で調査計画を返してください。\n\n"
            f"調査対象(JSON):\n{json.dumps(plan_context, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _code_prompt(
        hypothesis: StrategyHypothesis,
        start: date,
        end: date,
        schema: dict[str, Any],
        research_plan: dict[str, Any],
        previous_investigation: dict[str, Any] | None,
    ) -> str:
        return (
            f"対象期間: {start.isoformat()} ～ {end.isoformat()}\n"
            f"仮説: {hypothesis.title}\n"
            f"検証方法(追加分析案):\n{hypothesis.verification_method or '指定なし'}\n\n"
            f"調査計画(JSON):\n{json.dumps(research_plan, ensure_ascii=False, indent=2)}\n\n"
            f"前回調査(JSON):\n{json.dumps(previous_investigation, ensure_ascii=False, indent=2)}\n\n"
            "利用可能データソースのスキーマ(JSON):\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
            "生成コードは提示したquery_parquet/query_filter_events APIのみで読み取りを行い、"
            "集計結果をprintしてください。"
        )

    @staticmethod
    def _schema_for_prompt(schema: dict[str, Any]) -> dict[str, Any]:
        prompt_schema = dict(schema)
        parquet = schema.get("parquet")
        if isinstance(parquet, dict):
            prompt_schema["parquet"] = {
                key: value for key, value in parquet.items() if key != "sha256_by_file"
            }
        sqlite = schema.get("sqlite")
        if isinstance(sqlite, dict):
            prompt_schema["sqlite"] = {
                key: value for key, value in sqlite.items() if key != "sha256_files"
            }
        return prompt_schema

    @staticmethod
    def _judgment_prompt(
        hypothesis: StrategyHypothesis,
        research_plan: dict[str, Any],
        execution: StrategyAnalysisExecutionResult,
        external_data: dict[str, Any],
        previous_investigation: dict[str, Any] | None,
    ) -> str:
        result = {
            "hypothesis": hypothesis.title,
            "verification_method": hypothesis.verification_method,
            "research_plan": research_plan,
            "execution_status": execution.status,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "return_code": execution.return_code,
            "timed_out": execution.timed_out,
            "elapsed_seconds": round(execution.elapsed_seconds, 3),
            "external_data": external_data,
            "previous_investigation": previous_investigation,
        }
        return (
            "以下に含まれる内部実行結果とURL付き外部指数データだけを使って判定してください。"
            "取得していないニュースや、結果に存在しない事実を根拠にしないでください。\n"
            "\n分析結果(JSON):\n"
            f"{json.dumps(result, ensure_ascii=False, indent=2)}"
        )

    @staticmethod
    def _failure_judgment(reason: str) -> dict[str, Any]:
        return {
            "verdict": "追加データ必要",
            "confidence": "低",
            "reason": f"検証不能: {reason}",
            "evidence": [],
        }

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")

    def _verify_one(
        self,
        number: int,
        hypothesis: StrategyHypothesis,
        start: date,
        end: date,
        schema: dict[str, Any],
        hypothesis_directory: Path,
        previous_investigation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        hypothesis_directory.mkdir(parents=True)
        plan_prompt = self._research_plan_prompt(
            hypothesis, start, end, schema, previous_investigation
        )
        self._write_text(hypothesis_directory / "research_plan_system_prompt.txt", RESEARCH_PLAN_SYSTEM_PROMPT)
        self._write_text(hypothesis_directory / "research_plan_prompt.txt", plan_prompt)
        self._write_text(hypothesis_directory / "research_plan_response.txt", "")
        self._write_text(hypothesis_directory / "research_plan.json", "{}")
        self._write_text(hypothesis_directory / "code_generation_prompt.txt", "")
        self._write_text(hypothesis_directory / "code_generation_response.txt", "")
        self._write_text(hypothesis_directory / "external_data.json", "{}")
        self._write_text(hypothesis_directory / "previous_investigation.json", json.dumps(
            previous_investigation, ensure_ascii=False, indent=2
        ))
        self._write_text(hypothesis_directory / "judgment_prompt.txt", "")
        self._write_text(hypothesis_directory / "judgment_response.txt", "")
        self._write_text(hypothesis_directory / "analysis_code.py", "")

        generated_code = ""
        generation_response = ""
        plan_response = ""
        research_plan: dict[str, Any] = {}
        external_data: dict[str, Any] = {
            "provider": "not_requested",
            "results": [],
            "external_news": {"requested": False, "available": False},
        }
        self._write_text(
            hypothesis_directory / "code_generation_system_prompt.txt",
            CODE_GENERATION_SYSTEM_PROMPT,
        )
        self._write_text(
            hypothesis_directory / "judgment_system_prompt.txt",
            JUDGMENT_SYSTEM_PROMPT,
        )
        execution = StrategyAnalysisExecutionResult(
            stdout="", stderr="", elapsed_seconds=0.0, timed_out=False,
            return_code=0, status="not_requested",
        )
        judgment = self._failure_judgment("調査計画が完了していません")
        status = "検証不能"
        try:
            plan_response = self.client.complete(RESEARCH_PLAN_SYSTEM_PROMPT, plan_prompt)
            research_plan = parse_research_plan(plan_response)
            self._write_text(hypothesis_directory / "research_plan_response.txt", plan_response)
            self._write_text(
                hypothesis_directory / "research_plan.json",
                json.dumps(research_plan, ensure_ascii=False, indent=2),
            )

            if research_plan["external_indices"]:
                external_data = self.external_data_client.load_index_data(
                    research_plan["external_indices"], start, end
                )
                external_data["external_news"] = {
                    "requested": research_plan["external_news_needed"],
                    "available": False,
                    "reason": (
                        "一般ニュース検索は未構成です。Yahoo Finance指数データのみ取得対象です。"
                        if research_plan["external_news_needed"]
                        else None
                    ),
                }
            elif research_plan["external_news_needed"]:
                external_data["external_news"] = {
                    "requested": True,
                    "available": False,
                    "reason": "一般ニュース検索は未構成です。",
                }
            self._write_text(
                hypothesis_directory / "external_data.json",
                json.dumps(external_data, ensure_ascii=False, indent=2),
            )

            if research_plan["needs_internal_data"]:
                code_prompt = self._code_prompt(
                    hypothesis,
                    start,
                    end,
                    schema,
                    research_plan,
                    previous_investigation,
                )
                self._write_text(hypothesis_directory / "code_generation_prompt.txt", code_prompt)
                generation_response = self.client.complete(CODE_GENERATION_SYSTEM_PROMPT, code_prompt)
                generated_code = extract_python_code(generation_response)
                self._write_text(hypothesis_directory / "analysis_code.py", generated_code + "\n")
                execution = self.sandbox.run(generated_code, start, end)
                self._write_text(hypothesis_directory / "execution_stdout.txt", execution.stdout)
                self._write_text(hypothesis_directory / "execution_stderr.txt", execution.stderr)

            internal_failed = (
                research_plan["needs_internal_data"] and execution.status != "succeeded"
            )
            external_failed = bool(research_plan["external_indices"]) and not any(
                result.get("status") == "available"
                for result in external_data.get("results", [])
            )
            if internal_failed and (not research_plan["external_indices"] or external_failed):
                judgment = self._failure_judgment(execution.stderr or execution.status)
            else:
                judgment_prompt = self._judgment_prompt(
                    hypothesis,
                    research_plan,
                    execution,
                    external_data,
                    previous_investigation,
                )
                self._write_text(hypothesis_directory / "judgment_prompt.txt", judgment_prompt)
                judgment_response = self.client.complete(JUDGMENT_SYSTEM_PROMPT, judgment_prompt)
                self._write_text(hypothesis_directory / "judgment_response.txt", judgment_response)
                judgment = parse_judgment(judgment_response)
                status = "判定済み"
        except Exception as exc:
            logger.warning("仮説%dの検証に失敗しました: %s", number, exc)
            judgment = self._failure_judgment(str(exc))
            if execution.status == "not_requested":
                execution = StrategyAnalysisExecutionResult(
                    stdout="", stderr=str(exc), elapsed_seconds=0.0,
                    timed_out=False, return_code=None, status="generation_failed",
                )
        finally:
            self._write_text(hypothesis_directory / "research_plan_response.txt", plan_response)
            self._write_text(hypothesis_directory / "external_data.json", json.dumps(
                external_data, ensure_ascii=False, indent=2
            ))
            self._write_text(hypothesis_directory / "code_generation_response.txt", generation_response)
            self._write_text(hypothesis_directory / "execution_stdout.txt", execution.stdout)
            self._write_text(hypothesis_directory / "execution_stderr.txt", execution.stderr)
            execution_data = asdict(execution)
            (hypothesis_directory / "execution_metadata.json").write_text(
                json.dumps(execution_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raw_data = {
                "research_plan": research_plan,
                "stdout": execution.stdout,
                "stderr": execution.stderr,
                "status": execution.status,
                "return_code": execution.return_code,
                "timed_out": execution.timed_out,
                "elapsed_seconds": execution.elapsed_seconds,
                "external_data": external_data,
                "previous_investigation": previous_investigation,
            }
            (hypothesis_directory / "raw_data_for_judgment.json").write_text(
                json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (hypothesis_directory / "judgment.json").write_text(
                json.dumps(judgment, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        return {
            "id": f"{int(hypothesis.source_id):03d}" if hypothesis.source_id.isdigit() else f"{number:03d}",
            "title": hypothesis.title,
            "priority": hypothesis.priority,
            "verification_method": hypothesis.verification_method,
            "status": status,
            **judgment,
            "artifact_directory": hypothesis_directory.name,
        }

    def execute(self, input_report: Path) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("仮説検証には既存のLLM設定が必要です")
        input_report = Path(input_report).resolve()
        report_text = input_report.read_text(encoding="utf-8")
        start, end, all_hypotheses = parse_hypothesis_report(report_text)
        hypotheses = [hypothesis for hypothesis in all_hypotheses if hypothesis.priority == "高"]
        excluded_hypotheses = [hypothesis for hypothesis in all_hypotheses if hypothesis.priority != "高"]
        return self._execute_hypotheses(
            hypotheses,
            start,
            end,
            mode="pipeline",
            source_report=input_report,
            report_text=report_text,
            excluded_hypotheses=excluded_hypotheses,
        )

    def execute_selected(self, hypothesis_file: Path, hypothesis_id: str) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("仮説検証には既存のLLM設定が必要です")
        hypothesis_file = Path(hypothesis_file).resolve()
        report_text = hypothesis_file.read_text(encoding="utf-8")
        start, end, hypotheses = parse_hypothesis_report(report_text)
        requested_id = hypothesis_id.strip().lstrip("0") or "0"
        selected = [
            hypothesis for hypothesis in hypotheses
            if (hypothesis.source_id.lstrip("0") or "0") == requested_id
        ]
        if not selected:
            available = ", ".join(hypothesis.source_id for hypothesis in hypotheses)
            raise ValueError(f"仮説IDが見つかりません: {hypothesis_id} (利用可能: {available})")
        return self._execute_hypotheses(
            selected,
            start,
            end,
            mode="deep_dive",
            source_report=hypothesis_file,
            report_text=report_text,
        )

    def execute_adhoc(self, hypothesis_text: str, start: date, end: date) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("仮説検証には既存のLLM設定が必要です")
        hypothesis_text = hypothesis_text.strip()
        if not hypothesis_text:
            raise ValueError("アドホック仮説を空にはできません")
        if start > end:
            raise ValueError("アドホック調査の開始日は終了日以前にしてください")
        hypothesis = StrategyHypothesis(
            title=hypothesis_text[:160],
            verification_method=hypothesis_text,
            source_id="adhoc",
        )
        return self._execute_hypotheses(
            [hypothesis],
            start,
            end,
            mode="adhoc",
            source_report=None,
            report_text=None,
            adhoc_input=hypothesis_text,
        )

    def _execute_hypotheses(
        self,
        hypotheses: list[StrategyHypothesis],
        start: date,
        end: date,
        mode: str,
        source_report: Path | None,
        report_text: str | None,
        adhoc_input: str | None = None,
        excluded_hypotheses: list[StrategyHypothesis] | None = None,
    ) -> dict[str, Any]:
        timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
        run_directory = self.output_directory / timestamp
        run_directory.mkdir(parents=True, exist_ok=False)
        if report_text is not None:
            self._write_text(run_directory / "advisor_report.md", report_text)
        if adhoc_input is not None:
            self._write_text(run_directory / "adhoc_hypothesis.txt", adhoc_input)

        schema = self.sandbox.describe_data_sources(start, end)
        metadata = {
            "run_id": timestamp,
            "created_at": _format_datetime(datetime.now().astimezone()),
            "mode": mode,
            "input_report": str(source_report) if source_report is not None else None,
            "llm": {
                "model": getattr(self.client, "model", None),
                "api_url": getattr(self.client, "api_url", None),
                "secret_material_logged": False,
            },
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "hypothesis_count": len(hypotheses) + len(excluded_hypotheses or []),
            "executed_hypothesis_count": len(hypotheses),
            "excluded_hypothesis_count": len(excluded_hypotheses or []),
            "hypothesis_ids": [hypothesis.source_id for hypothesis in hypotheses]
            + [hypothesis.source_id for hypothesis in excluded_hypotheses or []],
            "previous_investigation_included": mode == "deep_dive",
            "timeout_seconds_per_hypothesis": self.sandbox.timeout_seconds,
            "python_version": platform.python_version(),
            "duckdb_version": duckdb.__version__,
            "sqlglot_version": sqlglot.__version__,
            "llm_request_timeout_seconds": getattr(self.client, "timeout", None),
            "sandbox_limits": {
                "max_code_bytes": MAX_CODE_BYTES,
                "max_ast_nodes": MAX_AST_NODES,
                "max_query_rows": MAX_QUERY_ROWS,
                "max_stdout_bytes": MAX_OUTPUT_BYTES,
                "duckdb_memory_limit": DUCKDB_MEMORY_LIMIT,
                "duckdb_threads": 1,
            },
            "data_access_policy": (
                "子プロセスの一時cwd、import/属性/ファイル/ネットワーク/API呼び出しを禁止するPython AST allowlist。"
                "SQLiteはmode=roで期間内行を取得し、Parquetは対象期間の明示ファイルのみ読み込む。"
                "生成コードから見えるのは許可テーブルへのSELECT関数のみ。DuckDB外部アクセスはロード後に無効化。"
            ),
            "external_web_search": "not_used",
            "conditional_external_data": "Yahoo Finance Chart API daily OHLC for allowlisted indices, only when selected by the research plan",
            "schema": schema,
        }
        (run_directory / "run_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        results = []
        hypothesis_root = run_directory / "hypotheses"
        hypothesis_root.mkdir()
        for number, hypothesis in enumerate(hypotheses, start=1):
            artifact_id = int(hypothesis.source_id) if hypothesis.source_id.isdigit() else number
            hypothesis_directory = hypothesis_root / f"hypothesis_{artifact_id:03d}"
            previous_investigation = (
                self._load_previous_investigation(hypothesis.title)
                if mode == "deep_dive"
                else None
            )
            result = self._verify_one(
                number,
                hypothesis,
                start,
                end,
                self._schema_for_prompt(schema),
                hypothesis_directory,
                previous_investigation,
            )
            results.append(result)

        for hypothesis in excluded_hypotheses or []:
            artifact_id = int(hypothesis.source_id) if hypothesis.source_id.isdigit() else len(results) + 1
            artifact_name = f"hypothesis_{artifact_id:03d}"
            artifact_directory = hypothesis_root / artifact_name
            artifact_directory.mkdir()
            skipped = {
                "id": f"{artifact_id:03d}",
                "title": hypothesis.title,
                "priority": hypothesis.priority,
                "status": f"未検証(優先度{hypothesis.priority}のため対象外)",
                "verdict": "未検証",
                "confidence": "低",
                "reason": "パイプラインは優先度「高」の仮説のみを自動検証します",
                "evidence": [],
                "artifact_directory": artifact_name,
            }
            (artifact_directory / "skipped.json").write_text(
                json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append(skipped)
        results.sort(key=lambda result: int(result["id"]) if str(result["id"]).isdigit() else 10**9)

        feedback = {
            "schema_version": 1,
            "mode": mode,
            "verified_at": metadata["created_at"],
            "source_report": source_report.name if source_report is not None else None,
            "period": metadata["period"],
            "hypotheses": [
                {
                    key: result[key]
                    for key in ("id", "title", "priority", "status", "verdict", "confidence", "reason", "evidence")
                }
                for result in results
            ],
        }
        (run_directory / "verified_hypotheses.json").write_text(
            json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        markdown_lines = [
            f"# 仮説検証レポート ({start.isoformat()} ～ {end.isoformat()})",
            "",
            f"監査データ: {run_directory}",
            "",
        ]
        for result in results:
            markdown_lines.extend([
                f"## {result['id']} {result['title']}",
                f"- 状態: {result['status']}",
                f"- 判定: {result['verdict']} (信頼度: {result['confidence']})",
                f"- 根拠: {result['reason']}",
                f"- 監査記録: hypotheses/{result['artifact_directory']}/",
                "",
            ])
        markdown = "\n".join(markdown_lines)
        (run_directory / "results.md").write_text(markdown, encoding="utf-8")
        return {
            "markdown": markdown,
            "run_directory": run_directory,
            "feedback_path": run_directory / "verified_hypotheses.json",
            "results": results,
        }

    def replay_saved_code(self, code_path: Path) -> dict[str, Any]:
        code_path = Path(code_path).resolve()
        if code_path.name != "analysis_code.py" or len(code_path.parents) < 3:
            raise ValueError("監査ディレクトリ内のanalysis_code.pyを指定してください")
        run_directory = code_path.parents[2]
        metadata_path = run_directory / "run_metadata.json"
        if not metadata_path.is_file():
            raise ValueError(f"元の監査メタデータがありません: {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        period = metadata.get("period")
        if not isinstance(period, dict):
            raise ValueError("監査メタデータに対象期間がありません")
        start = date.fromisoformat(period["start"])
        end = date.fromisoformat(period["end"])
        source_comparison = self.sandbox.compare_source_fingerprints(metadata.get("schema", {}))
        code = code_path.read_text(encoding="utf-8")
        execution = self.sandbox.run(code, start, end)

        replay_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
        replay_directory = run_directory / "replays" / replay_id
        replay_directory.mkdir(parents=True)
        shutil.copyfile(code_path, replay_directory / "analysis_code.py")
        (replay_directory / "execution_stdout.txt").write_text(execution.stdout, encoding="utf-8")
        (replay_directory / "execution_stderr.txt").write_text(execution.stderr, encoding="utf-8")
        replay_metadata = {
            "replay_id": replay_id,
            "replayed_at": _format_datetime(datetime.now().astimezone()),
            "original_run_id": metadata.get("run_id"),
            "source_code": str(code_path),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "source_fingerprints": source_comparison,
            "execution": asdict(execution),
        }
        (replay_directory / "replay_metadata.json").write_text(
            json.dumps(replay_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {
            "markdown": (
                f"# 監査コードの再実行\n\n"
                f"状態: {execution.status}\n"
                f"実行時間: {execution.elapsed_seconds:.3f}秒\n"
                f"タイムアウト: {'あり' if execution.timed_out else 'なし'}\n"
                f"元データ一致: {'はい' if source_comparison['matches'] else 'いいえ'}\n\n"
                f"標準出力:\n```text\n{execution.stdout}\n```\n"
            ),
            "replay_directory": replay_directory,
            "execution": execution,
            "source_fingerprints": source_comparison,
        }