"""
================================================================================
フィルタリングユースケース
スクリーニング結果から出来高急騰銘柄を抽出するアプリケーションロジック層です。
前日のスクリーニング結果に基づいて、本日の出来高変動を分析します。
================================================================================
"""
from datetime import datetime, timedelta
import logging

from src.domain.models import FilteringResult, ScoredCandidate
from src.domain.rules import calculate_volume_surge_ratio, select_top_n_by_surge_ratio
from src.config import config

logger = logging.getLogger(__name__)


class FilteringUseCase:
    """
    フィルタリング処理を実行するユースケッククラス。
    
    前日のスクリーニング結果に対して、本日の出来高を確認し、
    出来高急騰銘柄をトップNに絞り込みます。
    """
    
    def __init__(self, screening_repository, board_client, volume_client, result_repository, notifier=None):
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

    def execute(self) -> FilteringResult:
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
        today = datetime.now().date()
        previous_business_day = today - timedelta(days=1)
        # 土日を跨ぐ場合は前営業日に遡る
        while previous_business_day.weekday() >= 5:
            previous_business_day -= timedelta(days=1)
        screening = self.screening_repository.load_for_date(previous_business_day)
        scored = []
        candidates = []
        if screening:
            for symbol in screening.symbols:
                board = self.board_client.get_current_board(symbol)
                if not board:
                    logger.warning("フィルタリングスキップ: 銘柄=%s 理由=板情報なし", symbol)
                    continue
                if board.get("current_price") is None:
                    logger.warning("フィルタリングスキップ: 銘柄=%s 理由=現在値なし", symbol)
                    continue
                today_value = board.get("trading_value")
                if today_value is None and board.get("trading_volume") is not None:
                    today_value = float(board["current_price"]) * float(board["trading_volume"])
                if today_value is None:
                    logger.warning("フィルタリングスキップ: 銘柄=%s 理由=当日売買代金なし", symbol)
                    continue
                if hasattr(self.volume_client, "get_average_turnover"):
                    average = self.volume_client.get_average_turnover(symbol, 20)
                else:
                    average_volume = self.volume_client.get_average_volume(symbol, 20)
                    average = average_volume * float(board["current_price"]) if average_volume is not None else None
                if average is None:
                    logger.warning("フィルタリングスキップ: 銘柄=%s 理由=平均売買代金なし", symbol)
                    continue
                try:
                    surge_ratio = calculate_volume_surge_ratio(float(today_value), average)
                except ValueError:
                    logger.warning("平均出来高が0以下のため、%s をスキップします。", symbol)
                    continue
                scored.append(ScoredCandidate(symbol, float(today_value), average, surge_ratio))
            # 同時刻帯の過去分足が取得できないため、絶対倍率の足切りは行わず相対順位で選ぶ。
            candidates = scored
        symbols = select_top_n_by_surge_ratio(candidates, 10)
        result = FilteringResult(today.isoformat(), symbols, datetime.now().isoformat())
        self.result_repository.save(result)
        if self.notifier:
            excluded_by_surge_count = len(scored) - len(candidates)
            self._notify_completion(screening, symbols, scored, excluded_by_surge_count)
        return result

    def _notify_completion(self, screening, symbols, scored, excluded_by_surge_count: int = 0) -> None:
        if not screening:
            message = "【フィルタリング結果】\n前日のスクリーニング結果がないため、0銘柄です"
        else:
            ratios_by_symbol = {candidate.symbol: candidate.surge_ratio for candidate in scored}
            top_symbols = " / ".join(
                f"{symbol}(20日平均売買代金の{ratios_by_symbol[symbol]:.1f}倍)" for symbol in symbols[:5]
            )
            message = (
                "【フィルタリング結果】\n"
                f"採用銘柄: {len(symbols)}銘柄\n"
                f"スクリーニング対象: {len(screening.symbols)}件\n"
                f"出来高条件で除外: {excluded_by_surge_count}件"
            )
            if top_symbols:
                message += "\n上位銘柄:\n" + "\n".join(
                    f"- {symbol}(20日平均売買代金の{ratios_by_symbol[symbol]:.1f}倍)" for symbol in symbols[:5]
                )
        try:
            self.notifier(message)
        except Exception:
            logger.exception("フィルタリング完了通知に失敗しました。")
