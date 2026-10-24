"""Acceptance tests for sente.rules. These are the oracle: do not edit."""
import random

import pytest

from sente import rules


def test_split_premium_floor_case():
    sp = rules.split_premium(5_000)
    assert tuple(sp) == (100, 100)
    assert sp.total == 200


def test_split_premium_at_minimum_mint():
    sp = rules.split_premium(2_000)
    assert tuple(sp) == (100, 100)


def test_split_premium_proportional_case():
    sp = rules.split_premium(100_000)
    assert sp.exit_credit == 1_200
    assert sp.margin == 1_800
    assert sp.total == 3_000


def test_split_premium_caps_margin_not_exit_credit():
    sp = rules.split_premium(1_000_000)
    assert sp.exit_credit == 12_000
    assert sp.margin == 5_000
    assert sp.total == 17_000


def test_below_minimum_mint_rejected():
    with pytest.raises(ValueError):
        rules.split_premium(1_999)


@pytest.mark.parametrize(
    "exited,agent,rebate,margin,reserved",
    [
        (5_000, 100, 0, 100, 0),   # fully cashed out
        (3_000, 60, 16, 84, 40),   # partly cashed out
        (0, 0, 40, 60, 100),       # never cashed out
    ],
)
def test_settle_lot_matches_partner_document(exited, agent, rebate, margin, reserved):
    s = rules.settle_lot(5_000, 100, 100, exited)
    assert (s.agent_paid, s.rebate, s.margin_recognized, s.exit_credit_reserved) == (
        agent, rebate, margin, reserved,
    )


def test_settle_lot_rejects_bad_exited():
    with pytest.raises(ValueError):
        rules.settle_lot(5_000, 100, 100, 5_001)
    with pytest.raises(ValueError):
        rules.settle_lot(5_000, 100, 100, -1)


def test_commission_is_cumulative_and_exact():
    amount, credit = 5_000, 100
    assert rules.commission_cum(amount, credit, 3_000) == 60
    assert rules.commission_cum(amount, credit, 5_000) == 100
    # paying in pieces never drifts from paying at once
    total = 0
    before = 0
    for exited in (700, 1_300, 2_900, 4_100, 5_000):
        after = rules.commission_cum(amount, credit, exited)
        total += after - before
        before = after
    assert total == credit


def test_settlement_invariants_hold_for_many_lots():
    rng = random.Random(7)
    for _ in range(500):
        amount = rng.randrange(rules.MIN_MINT, 3_000_000)
        sp = rules.split_premium(amount)
        exited = rng.randrange(0, amount + 1)
        s = rules.settle_lot(amount, sp.exit_credit, sp.margin, exited)
        assert s.agent_paid + s.exit_credit_reserved == sp.exit_credit
        assert 0 <= s.rebate <= sp.margin
        assert s.margin_recognized == sp.margin - s.rebate
        # rebate never exceeds 20% of the premium paid
        assert s.rebate * 100 <= sp.total * rules.REBATE_PCT + 100
        # margin is never negative and exit credit is never spent twice
        assert s.margin_recognized >= 0 and s.exit_credit_reserved >= 0
