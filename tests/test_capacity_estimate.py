"""
Tests for apex.validation.capacity_estimate.

Behavioral guarantees locked in:
  - Hand-computed capacity for single-name and basket cases.
  - Participation cap scales capacity linearly and is clamped to (0, 1].
  - Turnover scales capacity inversely (more turnover => less capacity).
  - Degenerate / insufficient inputs fail closed (capacity 0.0).
"""

from __future__ import annotations

import math

from apex.validation.capacity_estimate import (
    CapacityEstimate,
    adv_dollars,
    capacity_from_adv,
    capacity_from_basket,
    tradable_per_day,
)


def test_adv_dollars_basic():
    # 1,000,000 shares * $50 = $50,000,000
    assert adv_dollars(1_000_000, 50.0) == 50_000_000.0


def test_adv_dollars_fails_closed_on_bad_input():
    assert adv_dollars(0, 50.0) == 0.0
    assert adv_dollars(1000, 0.0) == 0.0
    assert adv_dollars(-1000, 50.0) == 0.0
    assert adv_dollars(float("nan"), 50.0) == 0.0
    assert adv_dollars(1000, float("inf")) == 0.0


def test_tradable_per_day_basic():
    # 10% of $50,000,000 = $5,000,000
    assert tradable_per_day(50_000_000.0, 0.10) == 5_000_000.0


def test_tradable_per_day_clamps_cap_above_one():
    # cap > 1 is nonsensical; clamp to 1.0 => full ADV
    assert tradable_per_day(50_000_000.0, 2.5) == 50_000_000.0


def test_tradable_per_day_fails_closed():
    assert tradable_per_day(0.0, 0.10) == 0.0
    assert tradable_per_day(50_000_000.0, 0.0) == 0.0
    assert tradable_per_day(50_000_000.0, -0.10) == 0.0


def test_capacity_single_name_hand_computed():
    # ADV$ = 1,000,000 * $50 = $50,000,000
    # tradable/day = 10% => $5,000,000
    # turnover = 1.0 => capacity = $5,000,000
    est = capacity_from_adv(1_000_000, 50.0, participation_cap=0.10, turnover=1.0)
    assert isinstance(est, CapacityEstimate)
    assert est.daily_volume_usd == 50_000_000.0
    assert est.tradable_usd_per_day == 5_000_000.0
    assert est.capacity_usd == 5_000_000.0
    assert est.num_names == 1
    assert est.participation_cap == 0.10
    assert est.turnover == 1.0


def test_capacity_turnover_scales_inversely():
    # turnover 0.5 means only half the book trades per period => can deploy 2x.
    base = capacity_from_adv(1_000_000, 50.0, participation_cap=0.10, turnover=1.0)
    half = capacity_from_adv(1_000_000, 50.0, participation_cap=0.10, turnover=0.5)
    assert half.capacity_usd == 2.0 * base.capacity_usd
    assert half.capacity_usd == 10_000_000.0


def test_capacity_participation_cap_scales_linearly():
    low = capacity_from_adv(1_000_000, 50.0, participation_cap=0.05, turnover=1.0)
    high = capacity_from_adv(1_000_000, 50.0, participation_cap=0.20, turnover=1.0)
    # 0.20 cap is 4x the 0.05 cap.
    assert math.isclose(high.capacity_usd, 4.0 * low.capacity_usd)
    assert low.capacity_usd == 2_500_000.0
    assert high.capacity_usd == 10_000_000.0


def test_capacity_basket_sums_liquidity():
    # Two names: 1M*$50 = $50M ADV ; 500k*$20 = $10M ADV ; total $60M.
    # 10% => $6M/day ; turnover 1.0 => capacity $6M.
    est = capacity_from_basket(
        [1_000_000, 500_000],
        [50.0, 20.0],
        participation_cap=0.10,
        turnover=1.0,
    )
    assert est.daily_volume_usd == 60_000_000.0
    assert est.tradable_usd_per_day == 6_000_000.0
    assert est.capacity_usd == 6_000_000.0
    assert est.num_names == 2


def test_capacity_basket_skips_illiquid_names():
    # Second name has zero ADV; it contributes nothing and isn't counted.
    est = capacity_from_basket(
        [1_000_000, 0],
        [50.0, 20.0],
        participation_cap=0.10,
        turnover=1.0,
    )
    assert est.daily_volume_usd == 50_000_000.0
    assert est.capacity_usd == 5_000_000.0
    assert est.num_names == 1


def test_capacity_basket_mismatched_lengths_use_shorter():
    # prices shorter than adv_shares => only first name considered.
    est = capacity_from_basket(
        [1_000_000, 500_000],
        [50.0],
        participation_cap=0.10,
        turnover=1.0,
    )
    assert est.num_names == 1
    assert est.daily_volume_usd == 50_000_000.0


def test_capacity_fails_closed_on_zero_turnover():
    est = capacity_from_adv(1_000_000, 50.0, participation_cap=0.10, turnover=0.0)
    assert est.capacity_usd == 0.0
    assert est.turnover == 0.0
    # liquidity is still reported even though capacity is unbounded/zero.
    assert est.daily_volume_usd == 50_000_000.0


def test_capacity_fails_closed_on_no_liquidity():
    est = capacity_from_basket([0, 0], [10.0, 20.0], participation_cap=0.10)
    assert est.capacity_usd == 0.0
    assert est.daily_volume_usd == 0.0
    assert est.tradable_usd_per_day == 0.0
    assert est.num_names == 0


def test_capacity_clamps_cap_above_one():
    est = capacity_from_adv(1_000_000, 50.0, participation_cap=3.0, turnover=1.0)
    # cap clamped to 1.0 => full ADV tradable
    assert est.participation_cap == 1.0
    assert est.tradable_usd_per_day == 50_000_000.0
    assert est.capacity_usd == 50_000_000.0


def test_summary_is_readable():
    est = capacity_from_adv(1_000_000, 50.0, participation_cap=0.10, turnover=1.0)
    s = est.summary()
    assert "Capacity" in s
    assert "1 names" in s
