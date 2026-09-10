from __future__ import annotations

import logging
from datetime import date
from typing import Callable

from src.domain.models import MinuteBar
from src.infrastructure.persistence.minute_bar_repository import MinuteBarRepository

logger = logging.getLogger(__name__)


class MinuteBarBackfillUseCase:
    """
    Yahoo Financeの分足で、自前ポーリングで貯めた分足データを補強（アップグレード）するユースケース。

    Yahoo由来のデータはMinuteBarRepository側の優先度ルールにより、
    同じ時刻に自前ポーリング(source="poll")のデータがあっても上書きする。
    Yahooが持っていない時刻（例: 7日より前の日付）は、既存の自前データがそのまま残る。
    """

    def __init__(self, fetch_intraday_bars: Callable[[str, int], list[MinuteBar]], repository: MinuteBarRepository):
        self.fetch_intraday_bars = fetch_intraday_bars
        self.repository = repository

    def run(self, symbols: list[str], days: int = 7) -> dict[str, int]:
        """
        対象銘柄それぞれについてYahoo分足を取得し、日付ごとに振り分けて保存します。

        Returns:
            銘柄ごとに取り込んだ足の本数（呼び出し元でのログ・通知用）
        """
        imported_counts: dict[str, int] = {}
        for symbol in symbols:
            bars = self.fetch_intraday_bars(symbol, days)
            if not bars:
                logger.warning("Yahoo分足が取得できなかったためスキップします: 銘柄=%s", symbol)
                imported_counts[symbol] = 0
                continue

            for bar in bars:
                bar_date = date.fromisoformat(bar.time[:10])
                self.repository.append_bar(bar_date, symbol, bar)
            imported_counts[symbol] = len(bars)
            logger.info("Yahoo分足を取り込みました: 銘柄=%s 件数=%d", symbol, len(bars))

        return imported_counts
