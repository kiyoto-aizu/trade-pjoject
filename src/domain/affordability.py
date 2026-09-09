"""資金制約に関する純粋なドメインルール。"""

from src.domain.models import Candidate


def calc_affordable_stocks(
    candidates: list[Candidate],
    available_cash: float,
    unit_shares: int = 100,
) -> list[Candidate]:
    """指定資金で単元株を購入可能な候補を、入力順のまま返す。"""
    if available_cash < 0:
        raise ValueError("available_cash must be non-negative")
    if unit_shares <= 0:
        raise ValueError("unit_shares must be positive")
    return [
        candidate
        for candidate in candidates
        if candidate.current_price > 0
        and candidate.current_price * unit_shares <= available_cash
    ]