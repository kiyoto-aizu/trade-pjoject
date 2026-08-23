from datetime import datetime, timedelta

from src.domain.models import FilteringResult, ScoredCandidate
from src.domain.rules import calculate_volume_surge_ratio, select_top_n_by_surge_ratio


class FilteringUseCase:
    def __init__(self, screening_repository, board_client, volume_client, result_repository, notifier=None):
        self.screening_repository = screening_repository
        self.board_client = board_client
        self.volume_client = volume_client
        self.result_repository = result_repository
        self.notifier = notifier

    def execute(self) -> FilteringResult:
        today = datetime.now().date()
        previous_business_day = today - timedelta(days=1)
        while previous_business_day.weekday() >= 5:
            previous_business_day -= timedelta(days=1)
        screening = self.screening_repository.load_for_date(previous_business_day)
        scored = []
        if screening:
            for symbol in screening.symbols:
                board = self.board_client.get_current_board(symbol)
                average = self.volume_client.get_average_volume(symbol, 20)
                if not board or board.get("current_price") is None or average is None:
                    continue
                today_volume = board.get("trading_volume")
                if today_volume is None:
                    continue
                scored.append(ScoredCandidate(symbol, float(today_volume), average, calculate_volume_surge_ratio(float(today_volume), average)))
        symbols = select_top_n_by_surge_ratio(scored, 10)
        result = FilteringResult(today.isoformat(), symbols, datetime.now().isoformat())
        self.result_repository.save(result)
        if not screening and self.notifier:
            self.notifier("前日のスクリーニング結果がないため、フィルタ結果は0件です")
        return result
