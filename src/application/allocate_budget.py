"""候補銘柄への可変予算配分ユースケース。"""

from src.domain.affordability import calc_affordable_stocks
from src.domain.models import Allocation, Candidate


def allocate_budget(
    candidates: list[Candidate],
    total_cash: float,
    target_positions: int,
    unit_shares: int = 100,
    max_order_amount: float | None = None,
) -> list[Allocation]:
    """銘柄数と注文上限で制限した予算内に最大単元数を割り当てる。"""
    if total_cash < 0:
        raise ValueError("total_cash must be non-negative")
    if target_positions < 0:
        raise ValueError("target_positions must be non-negative")
    if unit_shares <= 0:
        raise ValueError("unit_shares must be positive")
    if max_order_amount is not None and max_order_amount < 0:
        raise ValueError("max_order_amount must be non-negative")

    if target_positions == 0:
        return []

    budget_per_position = total_cash / target_positions
    if max_order_amount is not None:
        budget_per_position = min(budget_per_position, max_order_amount)
    remaining_cash = total_cash
    allocations = []
    for candidate in calc_affordable_stocks(candidates, total_cash, unit_shares):
        available_budget = min(budget_per_position, remaining_cash)
        quantity = int(available_budget // (candidate.current_price * unit_shares)) * unit_shares
        if quantity < unit_shares:
            continue
        required_cash = candidate.current_price * quantity
        if required_cash > remaining_cash:
            continue
        allocations.append(Allocation(candidate.symbol, required_cash, quantity))
        remaining_cash -= required_cash
        if len(allocations) >= target_positions:
            break
    return allocations