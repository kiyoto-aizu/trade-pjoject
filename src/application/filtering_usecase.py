"""
================================================================================
フィルタリングユースケース
スクリーニング結果から出来高急騰銘柄を抽出するアプリケーションロジック層です。
前日のスクリーニング結果に基づいて、本日の出来高変動を分析します。
================================================================================
"""
from datetime import date, datetime, timedelta
import logging
from time import monotonic

from src.domain.models import FilteringResult, ScoredCandidate
from src.domain.rules import calculate_volume_surge_ratio, select_top_n_by_surge_ratio
from src.config import config
from src.infrastructure.calendar.japanese_calendar import is_trading_day
from src.infrastructure.persistence.decision_journal_repository import STAGE_FILTERING
from src.infrastructure.notification.slack_notify import format_result_notification

logger = logging.getLogger(__name__)


class FilteringDeadlineExceeded(RuntimeError):
    """価格帯別フィルタが設定された締め切りまでに完了しませんでした。"""


class FilteringUseCase:
    """
    フィルタリング処理を実行するユースケッククラス。
    
    前日のスクリーニング結果に対して、本日の出来高を確認し、
    出来高急騰銘柄をトップNに絞り込みます。
    """
    
    def __init__(
        self, screening_repository, board_client, volume_client, result_repository, notifier=None,
        decision_journal_repository=None,
        diagnostics_repository=None,
    ):
        """
        FilteringUseCaseを初期化します。
        
        Args:
            screening_repository: スクリーニング結果を取得するリポジトリ
            board_client: リアルタイム板情報を取得するクライアント
            volume_client: 過去の出来高データを取得するクライアント
            result_repository: フィルタリング結果を保存するリポジトリ
            notifier: 通知機能（オプション）
        """
        self.screening_repository = screening_repository
        self.board_client = board_client
        self.volume_client = volume_client
        self.result_repository = result_repository
        self.notifier = notifier
        self.decision_journal_repository = decision_journal_repository
        self.diagnostics_repository = diagnostics_repository

    def execute(
        self,
        target_date: date | None = None,
        price_cap: float | None = None,
        deadline_monotonic: float | None = None,
        price_band: str | None = None,
    ) -> FilteringResult:
        """
        フィルタリング処理を実行します。
        
        処理フロー：
        1. 前営業日のスクリーニング結果を取得
        2. 各銘柄の本日出来高と過去20営業日平均を比較
        3. 出来高急騰率でスコアリング
        4. 上位10銘柄に絞り込み
        5. 結果を保存して返却
        
        Returns:
            FilteringResultオブジェクト
        """
        run_started_at = datetime.now()
        today = target_date or run_started_at.date()
        try:
            price_cap = config.get_screening_price_cap() if price_cap is None else price_cap
        except Exception:
            price_cap = None
        previous_business_day = today - timedelta(days=1)
        # 土日・祝日・年末年始を跨ぐ場合は前営業日に遡る
        while not is_trading_day(previous_business_day):
            previous_business_day -= timedelta(days=1)
        screening = self.screening_repository.load_for_date(previous_business_day)
        scored = []
        candidates = []
        skips: list[tuple[str, str]] = []
        diagnostics = []
        reason_counts: dict[str, int] = {}
        timed_out = False
        next_unprocessed_index = 0

        def mark_skipped(record, diagnostic_reason, journal_reason):
            record["status"] = "skipped"
            record["reason_code"] = diagnostic_reason
            diagnostics.append(record)
            reason_counts[diagnostic_reason] = reason_counts.get(diagnostic_reason, 0) + 1
            skips.append((record["symbol"], journal_reason))

        if screening:
            for index, symbol in enumerate(screening.symbols):
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    timed_out = True
                    next_unprocessed_index = index
                    break
                next_unprocessed_index = index + 1
                record = {
                    "symbol": symbol,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "numerator": None,
                    "numerator_source": None,
                    "board_current_price": None,
                    "board_retrieved_at": None,
                    "board_fetch_duration_ms": None,
                    "average_turnover": None,
                    "average_days": None,
                    "average_includes_target_date": None,
                    "ratio": None,
                    "rank": None,
                    "selected": False,
                    "reason_code": None,
                    "status": "evaluated",
                }
                if target_date and hasattr(self.volume_client, "get_turnover_for_date"):
                    try:
                        today_value = self.volume_client.get_turnover_for_date(symbol, today)
                        average = self.volume_client.get_average_turnover_before(symbol, today, 20)
                    except Exception as exc:
                        record["error_type"] = type(exc).__name__
                        mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_NO_HISTORICAL_DATA")
                        logger.warning("過去日フィルタデータ取得失敗: 銘柄=%s 理由=%s", symbol, type(exc).__name__)
                        continue
                    if today_value is None or average is None:
                        mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_NO_HISTORICAL_DATA")
                        logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=過去データなし | 日付=%s", symbol, today)
                        continue
                    record["numerator"] = float(today_value)
                    record["numerator_source"] = "yahoo_daily_close_x_volume"
                    record["average_turnover"] = float(average)
                    record["average_includes_target_date"] = False
                    try:
                        surge_ratio = calculate_volume_surge_ratio(float(today_value), average)
                    except ValueError:
                        mark_skipped(record, "FILTER_AVERAGE_NON_POSITIVE", "FILTER_AVERAGE_NON_POSITIVE")
                        logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均出来高が0以下", symbol)
                        continue
                    scored.append(ScoredCandidate(symbol, float(today_value), average, surge_ratio))
                    record["ratio"] = surge_ratio
                    diagnostics.append(record)
                    continue

                board_started_at = datetime.now()
                try:
                    board = self.board_client.get_current_board(symbol)
                except Exception as exc:
                    record["board_retrieved_at"] = datetime.now().isoformat()
                    record["board_fetch_duration_ms"] = round(
                        (datetime.now() - board_started_at).total_seconds() * 1000, 3
                    )
                    record["error_type"] = type(exc).__name__
                    mark_skipped(record, "FILTER_BOARD_FETCH_FAILED", "FILTER_BOARD_MISSING")
                    logger.exception("板情報の取得に失敗しました: 銘柄=%s", symbol)
                    continue
                record["board_retrieved_at"] = datetime.now().isoformat()
                record["board_fetch_duration_ms"] = round(
                    (datetime.now() - board_started_at).total_seconds() * 1000, 3
                )
                if not board:
                    mark_skipped(record, "FILTER_BOARD_FETCH_FAILED", "FILTER_BOARD_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=板情報なし", symbol)
                    continue
                record["board_current_price"] = board.get("current_price")
                today_value = board.get("trading_value")
                volume = board.get("trading_volume")
                if today_value is not None:
                    record["numerator_source"] = "TradingValue"
                elif volume is not None and board.get("current_price") is not None:
                    today_value = float(board["current_price"]) * float(volume)
                    record["numerator_source"] = "current_price_x_cumulative_volume"
                if today_value is None:
                    missing_reason = (
                        "FILTER_CURRENT_PRICE_MISSING"
                        if volume is not None and board.get("current_price") is None
                        else "FILTER_TURNOVER_MISSING"
                    )
                    mark_skipped(record, missing_reason, "FILTER_TURNOVER_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=当日売買代金を計算できません", symbol)
                    continue
                record["numerator"] = float(today_value)
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    record["status"] = "partial"
                    record["reason_code"] = "FILTER_TIME_LIMIT"
                    diagnostics.append(record)
                    reason_counts["FILTER_TIME_LIMIT"] = reason_counts.get("FILTER_TIME_LIMIT", 0) + 1
                    timed_out = True
                    break
                try:
                    if hasattr(self.volume_client, "get_average_turnover_details"):
                        average_details = self.volume_client.get_average_turnover_details(symbol, 20, today)
                        average = average_details["average_turnover"]
                        record["average_days"] = average_details["average_days"]
                        record["average_includes_target_date"] = average_details[
                            "average_includes_target_date"
                        ]
                    elif hasattr(self.volume_client, "get_average_turnover"):
                        average = self.volume_client.get_average_turnover(symbol, 20)
                    else:
                        average_volume = self.volume_client.get_average_volume(symbol, 20)
                        average = average_volume * float(board["current_price"]) if average_volume is not None else None
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
                    mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING")
                    logger.exception("平均売買代金の取得に失敗しました: 銘柄=%s", symbol)
                    continue
                if average is None:
                    mark_skipped(record, "FILTER_AVERAGE_MISSING", "FILTER_AVERAGE_TURNOVER_MISSING")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均売買代金なし", symbol)
                    continue
                record["average_turnover"] = float(average)
                try:
                    surge_ratio = calculate_volume_surge_ratio(float(today_value), average)
                except ValueError:
                    mark_skipped(record, "FILTER_AVERAGE_NON_POSITIVE", "FILTER_AVERAGE_NON_POSITIVE")
                    logger.warning("フィルタリング評価対象外: 銘柄=%s 理由=平均出来高が0以下", symbol)
                    continue
                scored.append(ScoredCandidate(symbol, float(today_value), average, surge_ratio))
                record["ratio"] = surge_ratio
                if today_value == 0 and volume == 0:
                    record["reason_code"] = "FILTER_NO_TRADES"
                    reason_counts["FILTER_NO_TRADES"] = reason_counts.get("FILTER_NO_TRADES", 0) + 1
                diagnostics.append(record)
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    timed_out = True
                    break
            # 同時刻帯の過去分足が取得できないため、絶対倍率の足切りは行わず相対順位で選ぶ。
            candidates = scored
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            timed_out = True
        if timed_out and screening:
            for symbol in screening.symbols[next_unprocessed_index:]:
                diagnostics.append({
                    "symbol": symbol,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "numerator": None,
                    "numerator_source": None,
                    "board_current_price": None,
                    "board_retrieved_at": None,
                    "board_fetch_duration_ms": None,
                    "average_turnover": None,
                    "average_days": None,
                    "average_includes_target_date": None,
                    "ratio": None,
                    "rank": None,
                    "selected": False,
                    "reason_code": "FILTER_TIME_LIMIT",
                    "status": "not_evaluated",
                })
                reason_counts["FILTER_TIME_LIMIT"] = reason_counts.get("FILTER_TIME_LIMIT", 0) + 1
        symbols = select_top_n_by_surge_ratio(candidates, 10)
        result = FilteringResult(today.isoformat(), symbols, datetime.now().isoformat())
        if not timed_out:
            self.result_repository.save(result)
        ranked = sorted(candidates, key=lambda item: (-item.surge_ratio, item.symbol))
        rank_by_symbol = {candidate.symbol: rank for rank, candidate in enumerate(ranked, start=1)}
        selected_symbols = set(symbols)
        for record in diagnostics:
            if record["status"] == "evaluated":
                record["rank"] = rank_by_symbol.get(record["symbol"])
                record["selected"] = record["symbol"] in selected_symbols
        skipped_count = len(skips)
        summary = {
            "input_count": len(screening.symbols) if screening else 0,
            "evaluated_count": len(scored),
            "skipped_count": skipped_count,
            "selected_count": len(symbols),
            "reason_counts": reason_counts,
            "elapsed_ms": round((datetime.now() - run_started_at).total_seconds() * 1000, 3),
            "timed_out": timed_out,
            "stop_reason": "FILTER_TIME_LIMIT" if timed_out else None,
            "unprocessed_count": sum(record["status"] == "not_evaluated" for record in diagnostics),
        }
        logger.info(
            "フィルタリングサマリー: date=%s input=%d evaluated=%d skipped=%d selected=%d reasons=%s elapsed_ms=%.3f",
            today.isoformat(), summary["input_count"], summary["evaluated_count"], skipped_count,
            len(symbols), reason_counts, summary["elapsed_ms"],
        )
        if self.diagnostics_repository is not None:
            try:
                self.diagnostics_repository.save({
                    "date": today.isoformat(),
                    "generated_at": result.generated_at,
                    "price_band": price_band,
                    "price_cap": price_cap,
                    "result_saved": not timed_out,
                    "summary": summary,
                    "candidates": diagnostics,
                })
            except Exception:
                logger.exception("フィルタリング診断記録の保存に失敗しました。選定結果は保存済みです。")
        if timed_out:
            logger.warning(
                "価格帯別フィルタを時間切れで打ち切りました: band=%s evaluated=%d unprocessed=%d",
                price_band,
                len(scored) + len(skips),
                summary["unprocessed_count"],
            )
            raise FilteringDeadlineExceeded(f"{price_band or price_cap}円フィルタが時間切れで打ち切られました")
        self._journal_filter_stage(screening, len(scored), skips, symbols, today)
        if self.notifier:
            self._notify_completion(screening, symbols, scored, skipped_count, reason_counts)
        return result

    def _journal_filter_stage(self, screening, evaluated_count, skips, symbols, today) -> None:
        """日ごとの件数と、評価対象外の銘柄×日付×理由を記録する。失敗しても選定は継続する。"""
        if self.decision_journal_repository is None:
            return
        try:
            now = datetime.now()
            reason_counts: dict[str, int] = {}
            for symbol, code in skips:
                reason_counts[code] = reason_counts.get(code, 0) + 1
                self.decision_journal_repository.record(
                    STAGE_FILTERING, symbol, code, now, {"target_date": today.isoformat()}
                )
            self.decision_journal_repository.flush()
            self.decision_journal_repository.record_filter_stage_summary(
                now,
                len(screening.symbols) if screening else 0,
                evaluated_count,
                len(skips),
                len(symbols),
                reason_counts,
            )
        except Exception:
            logger.exception("フィルタリング段の判断記録の保存に失敗しました。")

    def _notify_completion(
        self, screening, symbols, scored, skipped_count: int = 0, reason_counts: dict[str, int] | None = None
    ) -> None:
        if not screening:
            message = format_result_notification(
                "銘柄選定",
                "フィルタリング",
                "前日の結果がないため完了しました。",
                ["採用銘柄数: 0件"],
            )
        else:
            ratios_by_symbol = {candidate.symbol: candidate.surge_ratio for candidate in scored}
            details = [
                f"採用銘柄数: {len(symbols)}件",
                f"入力銘柄数: {len(screening.symbols)}件",
                f"評価完了数: {len(scored)}件",
                f"評価対象外数: {skipped_count}件",
            ]
            if reason_counts:
                details.append(
                    "評価理由コード件数: "
                    + ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
                )
            if symbols:
                details.extend([
                    "上位銘柄:",
                    *[
                        f"- {symbol}(20日平均売買代金の{ratios_by_symbol[symbol]:.1f}倍)"
                        for symbol in symbols[:5]
                    ],
                ])
            anomaly = self._analyze_anomaly_if_needed(screening, symbols, scored, skipped_count)
            if anomaly:
                details.extend(["LLM異常検知(参考):", anomaly])
            message = format_result_notification(
                "銘柄選定",
                "フィルタリング",
                "フィルタリングが完了しました。",
                details,
            )
        try:
            self.notifier(message)
        except Exception:
            logger.exception("フィルタリング完了通知に失敗しました。")

    def _analyze_anomaly_if_needed(self, screening, symbols, scored, skipped_count: int) -> str | None:
        """採用件数が閾値を下回るなど普段と異なる可能性がある時だけLLMを呼び出し、クレジットを節約する。"""
        candidate_count = len(screening.symbols)
        if candidate_count > 0 and len(symbols) >= config.FILTERING_ANOMALY_MIN_SYMBOLS:
            return None
        try:
            from src.infrastructure.analysis.anomaly_analyzer import create_anomaly_analyzer

            analyzer = create_anomaly_analyzer()
            if not analyzer:
                return None
            summary = {
                "スクリーニング対象件数": candidate_count,
                "出来高条件クリア件数": len(scored),
                "採用件数": len(symbols),
                "評価対象外件数": skipped_count,
            }
            return analyzer.analyze_filtering(summary)
        except Exception:
            logger.exception("フィルタリング異常検知のLLM呼び出しに失敗しました。")
            return None
