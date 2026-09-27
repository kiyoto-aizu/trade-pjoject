from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Any

from src.infrastructure.analysis.strategy_hypothesis_analyzer import (
    SYSTEM_PROMPT,
    OpenAIStrategyHypothesisAnalyzer,
)
from src.infrastructure.analysis.strategy_hypothesis_context_repository import (
    StrategyHypothesisContextRepository,
    StrategyHypothesisReportRepository,
)

logger = logging.getLogger(__name__)

_USER_PROMPT_TEMPLATE = """以下のデータをもとに、現在の戦略の弱点について構造的な仮説を3〜5個挙げてください。
出力は次の形式のMarkdownで:

## 観察
- (事実のみ、箇条書き)

## 仮説
### 仮説1: [タイトル]
- 根拠:
- 反証しうるデータ:
- 検証方法(追加分析案):

## 優先度
- 高/中/低: 仮説N: その優先度にした理由
各箇条書きは必ず該当する仮説番号(仮説1、仮説2...)を1つ明記してください。"""


def build_strategy_hypothesis_prompt(context: dict[str, Any]) -> str:
    return (
        f"{_USER_PROMPT_TEMPLATE}\n\n"
        "past_strategy_verificationsを参照してください。棄却済み仮説は再提示しないでください。"
        "支持済み仮説は同じ形で繰り返さず、新しい未検証の観察に基づく発展形に限ります。"
        "追加データ必要または検証不能の仮説は、不足していた新データが今回含まれる場合のみ再検討してください。\n\n"
        "集約データ(JSON):\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def estimate_prompt_tokens(system_prompt: str, user_prompt: str) -> int:
    """トークナイザーを追加せず、UTF-8サイズから保守的に入力トークンを概算する。"""
    byte_count = len((system_prompt + user_prompt).encode("utf-8"))
    return math.ceil(byte_count / 2)


class GenerateStrategyHypothesesUseCase:
    def __init__(
        self,
        context_repository: StrategyHypothesisContextRepository,
        analyzer: OpenAIStrategyHypothesisAnalyzer,
        report_repository: StrategyHypothesisReportRepository,
    ):
        self.context_repository = context_repository
        self.analyzer = analyzer
        self.report_repository = report_repository

    def execute(self, start: date, end: date) -> dict[str, Any]:
        context = self.context_repository.load_context(start, end)
        user_prompt = build_strategy_hypothesis_prompt(context)
        estimated_tokens = estimate_prompt_tokens(SYSTEM_PROMPT, user_prompt)
        logger.info("戦略仮説アドバイザーの入力トークン概算: 約%d tokens", estimated_tokens)

        analysis = self.analyzer.analyze(user_prompt)
        if analysis is None:
            raise RuntimeError("OpenAI APIから分析結果を取得できませんでした")

        markdown = (
            f"# 戦略仮説アドバイザー\n\n"
            f"対象期間: {start.isoformat()} ～ {end.isoformat()}\n\n"
            f"入力トークン概算: 約{estimated_tokens} tokens (UTF-8バイト数÷2)\n\n"
            f"{analysis.rstrip()}\n"
        )
        output_path = self.report_repository.save(start, end, markdown)
        return {
            "markdown": markdown,
            "output_path": output_path,
            "estimated_input_tokens": estimated_tokens,
            "context": context,
        }