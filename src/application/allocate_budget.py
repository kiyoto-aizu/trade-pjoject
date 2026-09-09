"""候補銘柄への可変予算配分ユースケース。"""

from src.domain.affordability import calc_affordable_stocks
from src.domain.models import Allocation, Candidate


def allocate_budget(
    candidates: list[Candidate],
    total_cash: float,
    target_positions: int,
    unit_shares: int = 100,
) -> list[Allocation]:
    """順位順に単元分の予算を割り当て、購入不能な候補は次へ進む。"""
    if total_cash < 0:
        raise ValueError("total_cash must be non-negative")
    if target_positions < 0:
        raise ValueError("target_positions must be non-negative")
    if unit_shares <= 0:
        raise ValueError("unit_shares must be positive")

    remaining_cash = total_cash
    allocations = []
    for candidate in calc_affordable_stocks(candidates, total_cash, unit_shares):
        required_cash = candidate.current_price * unit_shares
        if required_cash > remaining_cash:
            continue
        allocations.append(Allocation(candidate.symbol, required_cash, unit_shares))
        remaining_cash -= required_cash
        if len(allocations) >= target_positions:
            break
    return allocations