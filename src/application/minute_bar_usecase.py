from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Optional

from src.domain.models import MinuteBar
from src.infrastructure.persistence.minute_bar_repository import MinuteBarRepository

logger = logging.getLogger(__name__)


class MinuteBarCollectionUseCase:
    """
    フィルタリング結果の対象銘柄について、1分間隔で板情報をポーリングし、
    その時点のCurrentPriceを1本の足として簡易的に保存するユースケース。

    真のOHLC（分内の高値・安値）は再現しない「簡易」方式。
    出来高は当日累積値(TradingVolume)の差分をそのポーリング区間の出来高として記録する。
    """

    def __init__(self, board_client, repository: MinuteBarRepository):
        self.board_client = board_client
        self.repository = repository
        # 銘柄ごとの直前ポーリング時点の累積出来高（差分計算用）
        self._last_cumulative_volume: dict[str, float] = {}

    def collect_once(self, symbols: list[str], now: datetime) -> None:
        """対象銘柄すべてについて板情報を1回取得し、分足として保存します。"""
        minute_key = now.strftime("%Y-%m-%dT%H:%M:00")
        target_date = now.date()

        for symbol in symbols:
            board = self.board_client.get_current_board(symbol)
            if not board or board.get("current_price") is None:
                logger.warning("板情報が取得できなかったためスキップします: 銘柄=%s", symbol)
                continue

            price = float(board["current_price"])
            cumulative_volume = board.get("trading_volume")
            cumulative_volume = float(cumulative_volume) if cumulative_volume is not None else None

            volume = None
            previous_cumulative = self._last_cumulative_volume.get(symbol)
            if cumulative_volume is not None and previous_cumulative is not None:
                volume = max(0.0, cumulative_volume - previous_cumulative)
            if cumulative_volume is not None:
                self._last_cumulative_volume[symbol] = cumulative_volume

            bar = MinuteBar(
                time=minute_key,
                price=price,
                cumulative_volume=cumulative_volume,
                volume=volume,
                source="poll",
            )
            self.repository.append_bar(target_date, symbol, bar)

    def run(
        self,
        symbols: list[str],
        is_trading_session: Callable[[datetime], bool],
        interval_seconds: int,
        now_provider: Optional[Callable[[], datetime]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        """市場時間中、interval_seconds間隔でcollect_onceを呼び続けます。"""
        now_provider = now_provider or datetime.now
        sleep = sleep or time.sleep

        if not symbols:
            logger.warning("収集対象の銘柄が空のため、分足収集を行わずに終了します。")
            return

        logger.info("分足収集を開始します: 対象銘柄=%s 間隔=%d秒", symbols, interval_seconds)
        while True:
            now = now_provider()
            if not is_trading_session(now):
                logger.info("市場時間外のため分足収集を終了します。")
                return
            try:
                self.collect_once(symbols, now)
            except Exception:  # noqa: BLE001 - 1回の失敗でループ全体を止めない
                logger.exception("分足収集中にエラーが発生しました。")
            sleep(interval_seconds)
