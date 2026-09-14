import pytest

from src.application.allocate_budget import allocate_budget
from src.domain.affordability import calc_affordable_stocks
from src.domain.models import Candidate


def candidates(*prices):
    return [Candidate(str(index), price, index) for index, price in enumerate(prices, 1)]


def test_affordability_keeps_ranked_candidates_that_fit_one_unit():
    result = calc_affordable_stocks(candidates(300, 900), 100_000)

    assert [candidate.symbol for candidate in result] == ["1", "2"]


def test_allocate_budget_uses_each_position_budget_for_multiple_units():
    result = allocate_budget(candidates(300, 600, 200), 100_000, 3)

    assert [(item.symbol, item.budget, item.quantity) for item in result] == [
        ("1", 30_000, 100),
        ("3", 20_000, 100),
    ]


def test_allocate_budget_uses_maximum_lots_within_position_budget():
    result = allocate_budget(candidates(23), 100_000, 3)

    assert [(item.budget, item.quantity) for item in result] == [(32_200, 1_400)]


def test_allocate_budget_caps_each_position_at_max_order_amount():
    result = allocate_budget(candidates(23), 100_000, 3, max_order_amount=30_000)

    assert [(item.budget, item.quantity) for item in result] == [(29_900, 1_300)]


def test_allocate_budget_skips_candidate_that_does_not_fit_remaining_cash():
    result = allocate_budget(candidates(300, 600, 200), 100_000, 3)

    assert [item.symbol for item in result] == ["1", "3"]


def test_allocate_budget_stops_when_remaining_cash_cannot_buy_one_unit():
    result = allocate_budget(candidates(900, 200), 100_000, 3)

    assert [item.symbol for item in result] == ["2"]


def test_budget_validation_fails_fast():
    with pytest.raises(ValueError):
        allocate_budget(candidates(100), -1, 1)