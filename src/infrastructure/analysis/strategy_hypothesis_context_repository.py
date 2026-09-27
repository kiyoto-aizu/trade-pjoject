from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_ADR_DECISION_LIMIT = 700


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _period_from_report(path: Path, report: dict[str, Any]) -> tuple[date, date] | None:
    period = report.get("period")
    if not isinstance(period, dict):
        period = {}
    start = next(
        (
            parsed
            for value in (
                period.get("start"), report.get("start_date"), report.get("period_start"),
                report.get("week_start"), report.get("month_start"),
            )
            if (parsed := _parse_date(value)) is not None
        ),
        None,
    )
    end = next(
        (
            parsed
            for value in (
                period.get("end"), report.get("end_date"), report.get("period_end"),
                report.get("week_end"), report.get("month_end"),
            )
            if (parsed := _parse_date(value)) is not None
        ),
        None,
    )
    if start is not None and end is not None:
        return start, end

    dates_in_name = [_parse_date(value) for value in _DATE_PATTERN.findall(path.name)]
    dates_in_name = [value for value in dates_in_name if value is not None]
    if len(dates_in_name) >= 2:
        return dates_in_name[0], dates_in_name[1]
    if len(dates_in_name) == 1:
        return dates_in_name[0], dates_in_name[0]
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("分析結果を読み込めません: %s (%s)", path, exc)
        return None
    if not isinstance(value, dict):
        logger.warning("分析結果のトップレベルがオブジェクトではありません: %s", path)
        return None
    return value


def _compact_daily(daily: Any) -> dict[str, Any] | None:
    if not isinstance(daily, dict):
        return None
    compact = {
        key: daily[key]
        for key in (
            "report_count", "order_count", "total_profit_loss", "kill_switch_days",
            "emergency_stop_days", "trading_modes", "operational_summary",
        )
        if key in daily
    }
    reports = daily.get("reports")
    if isinstance(reports, list):
        compact["reports"] = [
            {
                key: item[key]
                for key in (
                    "date", "order_count", "total_profit_loss", "trading_mode",
                    "market_assessment_status", "log_error_count", "position_count",
                )
                if key in item
            }
            for item in reports[:31]
            if isinstance(item, dict)
        ]
    return compact


def _compact_backtest(backtest: Any) -> dict[str, Any] | None:
    if not isinstance(backtest, dict):
        return None
    compact = {
        key: backtest[key]
        for key in (
            "run_count", "matching_period_run_count", "summary_status",
            "exact_period_run_available", "total_pnl", "total_trades",
        )
        if key in backtest
    }
    runs = backtest.get("runs")
    if isinstance(runs, list):
        compact["runs"] = [
            {
                key: run[key]
                for key in (
                    "generated_at", "period_start", "period_end", "total_pnl",
                    "total_trades", "win_rate", "max_drawdown", "final_position",
                )
                if key in run
            }
            for run in runs[:10]
            if isinstance(run, dict)
        ]
    return compact


def _compact_analysis(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    period = report.get("period") if isinstance(report.get("period"), dict) else {}
    backtest = _compact_backtest(report.get("backtest"))
    comparison = report.get("comparison")
    filter_events = report.get("filter_decision_events")
    compact: dict[str, Any] = {
        "source": path.name,
        "period": {
            "start": period.get("start") or report.get("week_start") or report.get("month_start"),
            "end": period.get("end") or report.get("week_end") or report.get("month_end"),
            "status": period.get("status"),
        },
        "daily": _compact_daily(report.get("daily")),
        "backtest": backtest,
    }
    if isinstance(comparison, dict):
        compact["comparison"] = {
            key: comparison[key]
            for key in ("available", "basis", "reason", "current", "previous")
            if key in comparison
        }
    if isinstance(filter_events, dict):
        compact["filter_decision_events"] = {
            key: filter_events[key]
            for key in ("count", "by_event_type")
            if key in filter_events
        }
    return compact


def _build_paper_backtest_comparison(
    analyses: list[dict[str, Any]], start: date, end: date
) -> dict[str, Any]:
    exact_reports = [
        analysis
        for analysis in analyses
        if analysis["period"]["start"] == start.isoformat()
        and analysis["period"]["end"] == end.isoformat()
    ]
    if len(exact_reports) != 1:
        return {
            "available": False,
            "reason": "指定期間と完全一致する週次/月次分析結果が1件に定まりません",
            "matching_analysis_count": len(exact_reports),
        }

    analysis = exact_reports[0]
    daily = analysis.get("daily") or {}
    backtest = analysis.get("backtest") or {}
    paper_pnl = daily.get("total_profit_loss")
    backtest_pnl = backtest.get("total_pnl")
    available = (
        backtest.get("exact_period_run_available") is True
        and isinstance(paper_pnl, (int, float))
        and isinstance(backtest_pnl, (int, float))
    )
    result: dict[str, Any] = {
        "available": available,
        "source": analysis["source"],
        "paper_report_count": daily.get("report_count"),
        "paper_order_count": daily.get("order_count"),
        "paper_total_profit_loss": paper_pnl,
        "backtest_total_pnl": backtest_pnl,
        "backtest_total_trades": backtest.get("total_trades"),
        "backtest_exact_period_run_available": backtest.get("exact_period_run_available", False),
        "basis_note": "異なる集計・約定条件の数値差は記述的な比較であり、同一条件の損益比較を意味しません",
    }
    if available:
        result["backtest_minus_paper_pnl"] = round(float(backtest_pnl) - float(paper_pnl), 2)
    else:
        result["reason"] = "期間一致バックテストまたはペーパー損益がありません"
    return result


def _limit_json_value(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, list):
        if depth >= 4:
            return f"[{len(value)} items omitted]"
        return [_limit_json_value(item, depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        if depth >= 4:
            return f"{{{len(value)} fields omitted}}"
        return {
            str(key)[:100]: _limit_json_value(item, depth + 1)
            for key, item in list(value.items())[:50]
        }
    return value


def _extract_section(lines: list[str], heading_pattern: re.Pattern[str]) -> list[str]:
    for index, line in enumerate(lines):
        if heading_pattern.match(line.strip()):
            section: list[str] = []
            for content_line in lines[index + 1 :]:
                if re.match(r"^##\s+", content_line):
                    break
                if not content_line.startswith("### "):
                    section.append(content_line.rstrip())
            return section
    return []


def _section_value(section: list[str]) -> str | None:
    for line in section:
        value = line.strip().lstrip("-*").strip()
        if value:
            return value
    return None


def _decision_summary(section: list[str], limit: int) -> str | None:
    blocks: list[str] = []
    current: list[str] = []
    for line in section:
        stripped = line.strip()
        if not stripped or stripped.startswith("|") or stripped.startswith("```"):
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if stripped.startswith("### "):
            continue
        if stripped.startswith(("- ", "* ")):
            current.append(stripped)
        else:
            current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    summary = " ".join(blocks).strip()
    summary = re.sub(r"[`*_]+", "", summary)
    if not summary:
        return None
    if len(summary) > limit:
        summary = summary[: limit - 1].rstrip() + "…"
    return summary


def extract_adr_summaries(adr_directory: Path) -> list[dict[str, str]]:
    """ADRのタイトル・ステータス・決定セクションのみを抽出する。"""
    summaries = []
    for path in sorted(adr_directory.glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            logger.warning("ADRを読み込めません: %s (%s)", path, exc)
            continue
        title = next(
            (match.group(1).strip() for line in lines if (match := re.match(r"^#\s+(.+)$", line))),
            None,
        )
        inline_status = next(
            (
                match.group(1).strip()
                for line in lines
                if (match := re.match(r"^\s*[-*]\s*ステータス\s*:\s*(.+)$", line))
            ),
            None,
        )
        status = inline_status or _section_value(
            _extract_section(lines, re.compile(r"^##\s*ステータス\s*$"))
        )
        decision = _decision_summary(
            _extract_section(lines, re.compile(r"^##\s*決定.*$")),
            _ADR_DECISION_LIMIT,
        )
        if title and status and decision:
            summaries.append({"title": title, "status": status, "decision": decision})
    return summaries


class StrategyHypothesisContextRepository:
    """既存の週次/月次・診断JSONとADRからプロンプト用の事実を読み込む。"""

    def __init__(
        self,
        weekly_directory: Path,
        monthly_directory: Path,
        analysis_directory: Path,
        adr_directory: Path,
        verification_directory: Path | None = None,
    ):
        self.weekly_directory = weekly_directory
        self.monthly_directory = monthly_directory
        self.analysis_directory = analysis_directory
        self.adr_directory = adr_directory
        self.verification_directory = verification_directory

    def _load_past_verification_results(self) -> list[dict[str, Any]]:
        if self.verification_directory is None or not self.verification_directory.exists():
            return []
        latest_by_title: dict[str, dict[str, Any]] = {}
        for path in sorted(
            self.verification_directory.glob("*/verified_hypotheses.json"), reverse=True
        ):
            report = _read_json(path)
            if report is None:
                continue
            items = report.get("hypotheses")
            if not isinstance(items, list):
                logger.warning("検証済み仮説一覧の形式が不正です: %s", path)
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("title"):
                    continue
                title = str(item["title"]).strip()
                if not title or title in latest_by_title:
                    continue
                evidence = item.get("evidence")
                latest_by_title[title] = {
                    "title": title,
                    "status": str(item.get("status") or "不明"),
                    "verdict": str(item.get("verdict") or "追加データ必要"),
                    "confidence": str(item.get("confidence") or "低"),
                    "reason": str(item.get("reason") or "")[:500],
                    "evidence": [str(value)[:200] for value in evidence[:3]]
                    if isinstance(evidence, list)
                    else [],
                    "verified_at": str(report.get("verified_at") or ""),
                }
        return list(latest_by_title.values())[:20]

    def _load_period_analyses(self, start: date, end: date) -> list[dict[str, Any]]:
        analyses = []
        for directory in (self.weekly_directory, self.monthly_directory):
            for path in sorted(directory.glob("*.json")):
                report = _read_json(path)
                if report is None:
                    continue
                period = _period_from_report(path, report)
                if period is None:
                    logger.warning("対象期間を判定できない分析結果を除外します: %s", path)
                    continue
                report_start, report_end = period
                if report_start <= end and start <= report_end:
                    analyses.append(_compact_analysis(path, report))
        return analyses

    def _load_optional_diagnostics(
        self, start: date, end: date, patterns: tuple[str, ...], label: str
    ) -> dict[str, Any]:
        reports: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for pattern in patterns:
            for path in sorted(self.analysis_directory.glob(pattern)):
                if path in seen:
                    continue
                seen.add(path)
                report = _read_json(path)
                if report is None:
                    continue
                period = _period_from_report(path, report)
                if period is None:
                    logger.warning("%sの対象期間を判定できないため除外します: %s", label, path)
                    continue
                if period[0] <= end and start <= period[1]:
                    reports.append({"source": path.name, "data": _limit_json_value(report)})
        return {"available": bool(reports), "reports": reports[:5]}

    def load_context(self, start: date, end: date) -> dict[str, Any]:
        if start > end:
            raise ValueError("--start-dateは--end-date以前の日付を指定してください")

        analyses = self._load_period_analyses(start, end)
        alignment = self._load_optional_diagnostics(
            start,
            end,
            ("*filter*entry*alignment*.json", "*analyze_filter_entry_alignment*.json"),
            "フィルタ整合性ログ",
        )
        regime_skip = self._load_optional_diagnostics(
            start,
            end,
            ("*regime*skip*severity*.json", "*analyze_regime_skip_severity*.json"),
            "レジームskip診断",
        )
        warnings = []
        if not analyses:
            warnings.append("対象期間に一致する週次/月次分析結果がありません")
        if not alignment["available"]:
            warnings.append(
                "対象期間のフィルタ整合性ログがありません（現行診断スクリプトは標準出力のみで、永続化ファイルを生成しません）"
            )
        if not regime_skip["available"]:
            warnings.append(
                "対象期間のレジームskip診断結果ファイルがありません（現行診断スクリプトは標準出力のみです）"
            )
        for warning in warnings:
            logger.warning("%s", warning)
        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "weekly_monthly_analyses": analyses,
            "paper_vs_backtest": _build_paper_backtest_comparison(analyses, start, end),
            "filter_entry_alignment": alignment,
            "regime_skip_severity": regime_skip,
            "past_adr_decisions": extract_adr_summaries(self.adr_directory),
            "past_strategy_verifications": self._load_past_verification_results(),
            "data_warnings": warnings,
        }


class StrategyHypothesisReportRepository:
    def __init__(self, output_directory: Path):
        self.output_directory = output_directory

    def save(self, start: date, end: date, markdown: str) -> Path:
        self.output_directory.mkdir(parents=True, exist_ok=True)
        path = self.output_directory / f"{start.isoformat()}_{end.isoformat()}.md"
        path.write_text(markdown, encoding="utf-8")
        return path