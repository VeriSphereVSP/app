# app/tests/test_smax_keeper.py — patch_smax_keeper
# Pure-logic tests for the keeper's decision function. No chain, no db, no web3:
# smax_keeper keeps heavy imports lazy precisely so this file runs anywhere.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smax_keeper import _decide  # noqa: E402

E = 10**18


def test_no_candidates_no_action():
    action, pid, _ = _decide(300 * E, 100, 100, [], -1)
    assert action is None and pid is None


def test_deviation_pokes_largest_deviating_post():
    # dormant giant (post 7) above sMax -> deviation poke, largest first
    cands = [(1, 50 * E), (7, 400 * E), (3, 120 * E)]
    action, pid, reason = _decide(300 * E, 100, 100, cands, -1)
    assert action == "deviation" and pid == 7
    assert "400" in reason


def test_deviation_not_epoch_gated():
    # even if decay was already poked this epoch, a deviation is still closed
    cands = [(7, 400 * E)]
    action, pid, _ = _decide(300 * E, 100, 100, cands, 100)
    assert action == "deviation" and pid == 7


def test_decay_materialization_once_per_epoch():
    # sMax above every candidate, bookkeeping stale -> decay poke on the leader
    cands = [(1, 50 * E), (2, 80 * E)]
    action, pid, _ = _decide(300 * E, 90, 100, cands, -1)
    assert action == "decay" and pid == 2
    # same epoch, already poked -> nothing
    action2, _, _ = _decide(300 * E, 90, 100, cands, 100)
    assert action2 is None
    # next epoch -> pokes again
    action3, _, _ = _decide(300 * E, 90, 101, cands, 100)
    assert action3 == "decay"


def test_honest_smax_no_action():
    # sMax == leader and bookkeeping current -> zero transactions
    cands = [(2, 300 * E), (1, 50 * E)]
    action, _, _ = _decide(300 * E, 100, 100, cands, -1)
    assert action is None


def test_smax_equal_to_leader_with_stale_epoch_no_decay_poke():
    # decay would floor at the leader immediately; sMax == leader already, so a
    # poke changes nothing -> stay quiet (strict > in the rule)
    cands = [(2, 300 * E)]
    action, _, _ = _decide(300 * E, 90, 100, cands, -1)
    assert action is None


def test_boundary_total_equal_to_smax_is_not_a_deviation():
    cands = [(2, 300 * E)]
    action, _, _ = _decide(300 * E, 100, 100, cands, -1)
    assert action is None
