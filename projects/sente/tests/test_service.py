"""Acceptance tests for sente.service. These are the oracle: do not edit."""
from datetime import datetime, timedelta, timezone

import pytest

from sente.service import EscrowError, InsufficientFunds, Sente


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self):
        return self.t

    def advance(self, days):
        self.t += timedelta(days=days)


def make():
    clock = FakeClock()
    s = Sente(clock=clock.now)
    for name in ("alice", "bob", "carol", "agent1"):
        s.register(name)
    return s, clock


def conserved(s):
    a = s.audit()
    assert a["bank"] == (
        a["wallets"] + a["escrow"] + a["exit_pool"] + a["deferred_margin"] + a["revenue"]
    ), a
    return a


def exit_cash(s, holder, amount, agent="agent1"):
    eid = s.request_exit(holder, agent, amount)
    s.confirm_exit(eid, "agent")
    assert s.confirm_exit(eid, "holder") == "released"
    return eid


# ---- minting -------------------------------------------------------------

def test_mint_splits_premium_and_conserves_money():
    s, _ = make()
    lot = s.mint("alice", 5_000)
    assert (lot.amount, lot.premium, lot.exit_credit, lot.margin) == (5_000, 200, 100, 100)
    a = conserved(s)
    assert s.balance("alice") == 5_000
    assert (a["bank"], a["exit_pool"], a["deferred_margin"], a["revenue"]) == (5_200, 100, 100, 0)


def test_mint_validations():
    s, _ = make()
    with pytest.raises(ValueError):
        s.mint("alice", 1_999)
    with pytest.raises(KeyError):
        s.mint("nobody", 5_000)


# ---- transfers -----------------------------------------------------------

def test_transfers_are_free_and_unlimited():
    s, _ = make()
    s.mint("alice", 5_000)
    before = s.audit()
    for src, dst in (("alice", "bob"), ("bob", "carol"), ("carol", "alice")) * 5:
        s.transfer(src, dst, 1_000 if src == "alice" else 1_000)
    after = conserved(s)
    assert s.balance("alice") + s.balance("bob") + s.balance("carol") == 5_000
    for key in ("bank", "exit_pool", "deferred_margin", "revenue"):
        assert after[key] == before[key]


def test_transfer_errors():
    s, _ = make()
    s.mint("alice", 5_000)
    with pytest.raises(InsufficientFunds):
        s.transfer("alice", "bob", 5_001)
    with pytest.raises(ValueError):
        s.transfer("alice", "bob", 0)
    with pytest.raises(ValueError):
        s.transfer("alice", "bob", -5)
    with pytest.raises(KeyError):
        s.transfer("alice", "nobody", 10)
    assert s.balance("alice") == 5_000


def test_transfer_is_idempotent_with_key():
    s, _ = make()
    s.mint("alice", 5_000)
    s.transfer("alice", "bob", 1_000, idem_key="k1")
    s.transfer("alice", "bob", 1_000, idem_key="k1")
    assert s.balance("bob") == 1_000 and s.balance("alice") == 4_000
    s.transfer("alice", "bob", 1_000, idem_key="k2")
    assert s.balance("bob") == 2_000


# ---- cash-out through escrow --------------------------------------------

def test_full_exit_recognises_margin_and_pays_commission():
    s, _ = make()
    s.mint("alice", 5_000)
    eid = s.request_exit("alice", "agent1", 5_000)
    assert s.escrow_status(eid) == "locked"
    assert s.balance("alice") == 0
    conserved(s)
    assert s.confirm_exit(eid, "agent") == "locked"
    assert s.confirm_exit(eid, "agent") == "locked"  # idempotent
    assert s.confirm_exit(eid, "holder") == "released"
    assert s.confirm_exit(eid, "holder") == "released"  # idempotent after release
    assert s.balance("agent1") == 5_100  # cash value plus commission
    a = conserved(s)
    assert (a["exit_pool"], a["deferred_margin"], a["revenue"], a["escrow"]) == (0, 0, 100, 0)


def test_partial_exit_pays_pro_rata_commission():
    s, _ = make()
    s.mint("alice", 5_000)
    s.transfer("alice", "bob", 3_000)
    exit_cash(s, "bob", 3_000)
    assert s.balance("agent1") == 3_060
    a = conserved(s)
    assert (a["exit_pool"], a["deferred_margin"], a["revenue"]) == (40, 100, 0)
    assert s.lots()[0].exited == 3_000


def test_exits_consume_oldest_lot_first():
    s, clock = make()
    s.mint("alice", 5_000)
    clock.advance(1)
    s.mint("carol", 2_000)
    s.transfer("carol", "alice", 2_000)
    exit_cash(s, "alice", 6_000)
    assert s.balance("agent1") == 6_150  # 100 + 50 commission
    lot1, lot2 = s.lots()
    assert (lot1.exited, lot2.exited) == (5_000, 1_000)
    a = conserved(s)
    assert (a["exit_pool"], a["deferred_margin"], a["revenue"]) == (50, 100, 100)
    assert s.balance("alice") == 1_000


def test_cancel_returns_funds_and_blocks_confirmation():
    s, _ = make()
    s.mint("alice", 5_000)
    eid = s.request_exit("alice", "agent1", 2_000)
    assert s.balance("alice") == 3_000
    assert s.cancel_exit(eid) == "cancelled"
    assert s.cancel_exit(eid) == "cancelled"
    assert s.balance("alice") == 5_000
    with pytest.raises(EscrowError):
        s.confirm_exit(eid, "agent")
    conserved(s)


def test_cannot_cancel_after_release():
    s, _ = make()
    s.mint("alice", 5_000)
    eid = exit_cash(s, "alice", 1_000)
    with pytest.raises(EscrowError):
        s.cancel_exit(eid)


def test_exit_request_validations():
    s, _ = make()
    s.mint("alice", 5_000)
    with pytest.raises(InsufficientFunds):
        s.request_exit("alice", "agent1", 5_001)
    with pytest.raises(ValueError):
        s.request_exit("alice", "agent1", 0)
    with pytest.raises(KeyError):
        s.request_exit("alice", "ghost", 100)
    eid = s.request_exit("alice", "agent1", 100)
    with pytest.raises(ValueError):
        s.confirm_exit(eid, "stranger")


def test_agent_redemption_settles_bank_cash():
    s, _ = make()
    s.mint("alice", 5_000)
    exit_cash(s, "alice", 5_000)
    bank_before = s.audit()["bank"]
    s.agent_redeem("agent1", 5_100)
    assert s.balance("agent1") == 0
    assert s.audit()["bank"] == bank_before - 5_100
    conserved(s)
    with pytest.raises(InsufficientFunds):
        s.agent_redeem("agent1", 1)


# ---- 12-month checkpoint and rebate --------------------------------------

def test_checkpoint_partial_exit_pays_rebate_once():
    s, clock = make()
    s.mint("alice", 5_000)
    s.transfer("alice", "bob", 3_000)
    exit_cash(s, "bob", 3_000)
    clock.advance(364)
    assert s.run_checkpoint() == []
    clock.advance(1)
    result = s.run_checkpoint()
    assert len(result) == 1
    assert result[0]["buyer"] == "alice" and result[0]["rebate"] == 16
    assert s.balance("alice") == 2_016
    a = conserved(s)
    assert (a["revenue"], a["deferred_margin"], a["exit_pool"]) == (84, 0, 40)
    assert s.run_checkpoint() == []


def test_checkpoint_never_exited_lot():
    s, clock = make()
    s.mint("alice", 5_000)
    clock.advance(365)
    result = s.run_checkpoint()
    assert result[0]["rebate"] == 40 and result[0]["margin_recognized"] == 60
    assert s.balance("alice") == 5_040
    a = conserved(s)
    assert (a["revenue"], a["deferred_margin"], a["exit_pool"]) == (60, 0, 100)


def test_exit_credit_stays_reserved_after_checkpoint():
    s, clock = make()
    s.mint("alice", 5_000)
    s.transfer("alice", "bob", 3_000)
    exit_cash(s, "bob", 3_000)
    clock.advance(365)
    s.run_checkpoint()
    exit_cash(s, "alice", 2_000)  # the rest leaves later; coverage still holds
    assert s.balance("agent1") == 3_060 + 2_040
    a = conserved(s)
    assert (a["exit_pool"], a["revenue"]) == (0, 84)


def test_fully_exited_lot_is_ignored_by_checkpoint():
    s, clock = make()
    s.mint("alice", 5_000)
    exit_cash(s, "alice", 5_000)
    clock.advance(400)
    assert s.run_checkpoint() == []
    assert conserved(s)["revenue"] == 100
