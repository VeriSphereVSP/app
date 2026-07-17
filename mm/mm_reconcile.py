"""app/mm/mm_reconcile.py — F-7 non-atomic-trade recording + reconciliation.

MM trades move funds in multiple on-chain legs with no atomicity (see
LAUNCH-RISK-AUDIT 2026-07-06 §2.1). This module has two halves:

  RECORDING (called from the money routes, mm_routes.py):
    - open_partial(...)      : write an 'open' row the moment we're about to move
                              the first leg, so a crash mid-flow leaves a durable trace.
    - mark_leg(...)          : record each leg's hash as it lands.
    - close_partial_ok(...)  : the trade completed wholly -> mark 'noop' (nothing to cure).
    Recording uses its OWN db session (never the request's), committed eagerly, so a
    later failure that rolls back the request transaction cannot erase the record.

  RECONCILING (called from the worker loop, observe+ACT):
    - reconcile_once()       : find 'open' rows, read on-chain truth, and either
                              complete-forward, refund-back, or mark already-whole.
    Fund movement is gated by MM_RECONCILE_ENABLED (default ON per the 2026-07-15
    decision); set it false to run detect+alert-only without a redeploy.

Cure policy (asymmetric — the two sides fail into opposite custody states):
    buy  partial: user USDC reached the MM but VSP payout failed. The user paid and
                  holds nothing. Default cure = REFUND the USDC principal(+fee) back
                  to the user (MM_RECONCILE_BUY_CURE=refund). 'complete' would send
                  the VSP instead; refund is the safer default because the usual cause
                  is MM VSP inventory shortfall, which 'complete' cannot fix anyway.
    sell partial: user VSP reached the MM but the USDC payout failed. The MM holds the
                  VSP and the price is locked. Default cure = COMPLETE-forward the USDC
                  payout (MM_RECONCILE_SELL_CURE=complete). 'refund' would send the VSP
                  back instead.

Every cure is: verified against on-chain balances first (a receipt-timeout leg may
have actually landed), bounded by MM_RECONCILE_MAX_USDC / _MAX_VSP, idempotent (the
unique idem_key + per-leg hash columns mean re-runs never double-pay), and alerted.

rev2 (patch_alerthygiene_rev2, 2026-07-16):
  * The MM_RECONCILE_ENABLED gate is checked FIRST for a true partial — observe-only
    mode now never transitions rows (they stay 'open' with a note; one would-act
    alert on transition, no repeats, no attempts consumed).
  * Insolvency is NON-terminal: an underfunded MM leaves the row 'open' with an
    alert-on-transition note and retries next pass, so a top-up cures it without
    manual state surgery. over_cap remains terminal (needs a human).

rev3 (patch_sellfee_recording_rev3, 2026-07-17):
  * Fee-leg aware. Sell fees flow MM->sink as a third leg (now inside the F-7
    envelope). A row whole-for-the-user but missing an expected fee leg
    reconciles with cure_kind='already_whole_fee_leg_missing' + info alert; the
    auto-reconciler never moves MM->sink funds itself (user-money mandate only).
  * Buy refunds are fee-conditional: fee is returned only if its leg landed
    (was unconditional -> over-refunded when the buy's fee leg never executed).
"""
import logging
import os
import time

from sqlalchemy import text as sql_text
from web3 import Web3

from db import get_session_factory

logger = logging.getLogger("mm_reconcile")

# ── config ────────────────────────────────────────────────────────────────
def _bool_env(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _f(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

RECONCILE_ENABLED = _bool_env("MM_RECONCILE_ENABLED", True)   # fund-moving gate (default ON)
BUY_CURE = os.getenv("MM_RECONCILE_BUY_CURE", "refund").strip().lower()    # refund|complete
SELL_CURE = os.getenv("MM_RECONCILE_SELL_CURE", "complete").strip().lower()  # complete|refund
MAX_USDC = _f("MM_RECONCILE_MAX_USDC", 5000.0)   # per-cure cap, USDC
MAX_VSP = _f("MM_RECONCILE_MAX_VSP", 50000.0)    # per-cure cap, VSP
MAX_ATTEMPTS = int(_f("MM_RECONCILE_MAX_ATTEMPTS", 5))
SCAN_LIMIT = int(_f("MM_RECONCILE_SCAN_LIMIT", 20))
MICRO = 10 ** 6
WEI = 10 ** 18

_ERC20_BAL_ABI = [{
    "constant": True, "inputs": [{"name": "a", "type": "address"}],
    "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]


# ── idempotency key ────────────────────────────────────────────────────────
def make_idem_key(side: str, user_address: str, qty_vsp: float, when: float | None = None) -> str:
    """Deterministic per (user, side, qty, minute-bucket) so a retried HTTP request
    reuses the same row instead of inserting a second one."""
    bucket = int((when if when is not None else time.time()) // 60)
    return f"{side}:{user_address.lower()}:{qty_vsp:.8f}:{bucket}"


# ── RECORDING (called from money routes; OWN session, eager commit) ────────
def open_partial(*, side, user_address, qty_vsp, reserves_micro, fee_micro, vsp_wei, idem_key):
    """Insert (or fetch) the 'open' row before the first leg moves. Returns row id.
    ON CONFLICT the existing id is returned so a retry reuses it."""
    sf = get_session_factory()
    db = sf()
    try:
        row = db.execute(sql_text(
            "INSERT INTO mm_partial_trade "
            "  (side, user_address, qty_vsp, reserves_micro, fee_micro, vsp_wei, state, idem_key) "
            "VALUES (:side, :ua, :qty, :res, :fee, :vsp, 'open', :idem) "
            "ON CONFLICT (idem_key) DO UPDATE SET updated_at = now() "
            "RETURNING id"
        ), {"side": side, "ua": user_address.lower(), "qty": qty_vsp,
            "res": int(reserves_micro), "fee": int(fee_micro), "vsp": str(int(vsp_wei)),
            "idem": idem_key}).scalar()
        db.commit()
        return row
    except Exception as e:
        db.rollback()
        logger.warning("open_partial failed (%s): %s", idem_key, e)
        return None
    finally:
        db.close()


def mark_leg(row_id, leg_col: str, tx_hash: str):
    """Record a landed leg's hash. leg_col in
    {leg_principal_in_tx, leg_fee_in_tx, leg_payout_out_tx}."""
    if row_id is None or leg_col not in (
        "leg_principal_in_tx", "leg_fee_in_tx", "leg_payout_out_tx"
    ):
        return
    sf = get_session_factory()
    db = sf()
    try:
        db.execute(sql_text(
            f"UPDATE mm_partial_trade SET {leg_col} = :h, updated_at = now() WHERE id = :id"
        ), {"h": tx_hash, "id": row_id})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("mark_leg failed (id=%s col=%s): %s", row_id, leg_col, e)
    finally:
        db.close()


def close_partial_ok(row_id):
    """All legs landed -> nothing to reconcile. Mark 'noop' so the scan skips it."""
    if row_id is None:
        return
    sf = get_session_factory()
    db = sf()
    try:
        db.execute(sql_text(
            "UPDATE mm_partial_trade SET state = 'noop', cure_kind = 'no_move', "
            "updated_at = now() WHERE id = :id AND state = 'open'"
        ), {"id": row_id})
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("close_partial_ok failed (id=%s): %s", row_id, e)
    finally:
        db.close()


# ── RECONCILING (worker) ───────────────────────────────────────────────────
def _alert(kind, message, **fields):
    try:
        import notify
        notify.send_alert(kind, message, **fields)
    except Exception as e:
        logger.warning("reconcile alert delivery failed: %s", e)


def _w3():
    from mm_wallet import w3
    return w3


def _balance_of(w3, token_addr, holder) -> int:
    c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_ERC20_BAL_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(holder)).call()


def reconcile_once():
    """One reconciliation pass. Returns a small summary dict. Never raises."""
    sf = get_session_factory()
    db = sf()
    summary = {"scanned": 0, "reconciled": 0, "refunded": 0, "failed": 0, "noop": 0}
    try:
        rows = db.execute(sql_text(
            "SELECT id, side, user_address, qty_vsp, reserves_micro, fee_micro, vsp_wei, "
            "       leg_principal_in_tx, leg_fee_in_tx, leg_payout_out_tx, attempts, last_error "
            "FROM mm_partial_trade WHERE state = 'open' "
            "ORDER BY created_at ASC LIMIT :lim"
        ), {"lim": SCAN_LIMIT}).mappings().all()
        summary["scanned"] = len(rows)
        for r in rows:
            try:
                _reconcile_row(db, r, summary)
            except Exception as e:
                logger.exception("reconcile row %s crashed", r["id"])
                db.rollback()
                _bump_attempt(db, r["id"], str(e))
        return summary
    except Exception as e:
        db.rollback()
        logger.warning("reconcile_once failed: %s", e)
        return {"error": str(e)}
    finally:
        db.close()


def _bump_attempt(db, row_id, err):
    db.execute(sql_text(
        "UPDATE mm_partial_trade SET attempts = attempts + 1, last_error = :e, "
        "updated_at = now() WHERE id = :id"
    ), {"e": (err or "")[:500], "id": row_id})
    db.commit()


def _note_open(db, r, note, alert_kind=None, alert_msg=None, **fields):
    """Leave the row 'open' with a note; alert only on TRANSITION into this note
    (repeat passes with the same note are silent). Does not consume attempts, so
    self-healing conditions (gate off, MM top-up pending) never escalate to
    'failed' by mere passage of time. patch_alerthygiene_rev2."""
    if (r["last_error"] or "") == note:
        return
    db.execute(sql_text(
        "UPDATE mm_partial_trade SET last_error = :n, updated_at = now() WHERE id = :id"
    ), {"n": note, "id": r["id"]})
    db.commit()
    if alert_kind:
        _alert(alert_kind, alert_msg or note, **fields)


def _set_state(db, row_id, state, *, cure_tx=None, cure_kind=None):
    db.execute(sql_text(
        "UPDATE mm_partial_trade SET state = :s, cure_tx = :t, cure_kind = :k, "
        "attempts = attempts + 1, updated_at = now() WHERE id = :id"
    ), {"s": state, "t": cure_tx, "k": cure_kind, "id": row_id})
    db.commit()


def _reconcile_row(db, r, summary):
    from config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS
    row_id = r["id"]
    side = r["side"]
    user = r["user_address"]

    if r["attempts"] >= MAX_ATTEMPTS:
        _set_state(db, row_id, "failed", cure_kind="max_attempts")
        summary["failed"] += 1
        _alert("mm_reconcile_failed",
               f"partial {side} trade {row_id} exhausted {MAX_ATTEMPTS} cure attempts — MANUAL",
               user=user, trade_id=row_id)
        return

    principal_in = r["leg_principal_in_tx"]   # first leg (into MM)
    payout_out = r["leg_payout_out_tx"]       # last leg (out to user)

    # Case A: no leg landed at all -> nothing moved, close harmlessly.
    if not principal_in and not payout_out:
        _set_state(db, row_id, "noop", cure_kind="no_move")
        summary["noop"] += 1
        return

    # Case B: both principal-in and payout-out landed -> the trade is whole FOR
    # THE USER; the route's failure was after their money finished. Reconcile.
    # rev3: if a fee leg was expected (fee_micro>0) but never landed, the user
    # is whole yet the SINK is short — reconcile with a distinct cure_kind and
    # an info alert so the operator settles the internal MM->sink transfer
    # manually. Deliberately no auto-cure: MM->sink is not user money.
    if principal_in and payout_out:
        if int(r["fee_micro"]) > 0 and not r["leg_fee_in_tx"]:
            _set_state(db, row_id, "reconciled", cure_kind="already_whole_fee_leg_missing")
            summary["reconciled"] += 1
            _alert("mm_reconcile_fee_leg_missing",
                   f"{side} {row_id}: user made whole but the fee leg "
                   f"({int(r['fee_micro'])/MICRO:.2f} USDC -> sink) never landed — "
                   f"settle MM->sink manually",
                   user=user, trade_id=row_id)
            return
        _set_state(db, row_id, "reconciled", cure_kind="already_whole")
        summary["reconciled"] += 1
        return

    # Case C: principal landed, payout did NOT (the true F-7 partial).
    # patch_alerthygiene_rev2: check the fund-moving gate FIRST — observe-only
    # mode must not evaluate solvency/caps or transition rows; it notes, alerts
    # once, and leaves the row 'open' for whenever the gate flips on.
    if not RECONCILE_ENABLED:
        planned = (BUY_CURE if side == "buy" else SELL_CURE)
        _note_open(db, r, "gated: MM_RECONCILE_ENABLED=false",
                   alert_kind="mm_reconcile_would_act",
                   alert_msg=(f"[disabled] {side} partial {row_id}: cure pending "
                              f"(policy={planned}); set MM_RECONCILE_ENABLED=true to act"),
                   user=user, trade_id=row_id)
        return

    w3 = _w3()

    if side == "buy":
        # principal_in = user USDC -> MM (landed). payout = MM VSP -> user (missing?)
        # A receipt-timeout payout can't be re-derived by balance alone, so we cure
        # by policy. Default: REFUND the USDC principal (+fee) back to the user.
        if BUY_CURE == "complete":
            vsp_wei = int(r["vsp_wei"])
            if vsp_wei / WEI > MAX_VSP:
                _set_state(db, row_id, "failed", cure_kind="over_cap")
                summary["failed"] += 1
                _alert("mm_reconcile_over_cap",
                       f"buy {row_id} complete needs {vsp_wei/WEI:.2f} VSP > cap {MAX_VSP}",
                       user=user, trade_id=row_id)
                return
            mm_vsp = _balance_of(w3, VSP_ADDRESS, MM_ADDRESS)
            if mm_vsp < vsp_wei:
                _note_open(db, r, "insolvent: MM lacks VSP to complete",
                           alert_kind="mm_reconcile_insolvent",
                           alert_msg=(f"buy {row_id}: MM lacks VSP to complete "
                                      f"({mm_vsp/WEI:.2f} < {vsp_wei/WEI:.2f}); top up MM or "
                                      f"switch MM_RECONCILE_BUY_CURE=refund — will retry"),
                           user=user, trade_id=row_id)
                return
            from mm.erc20 import transfer
            h = transfer(VSP_ADDRESS, user, vsp_wei)
            mark_leg(row_id, "leg_payout_out_tx", h)
            _set_state(db, row_id, "reconciled", cure_tx=h, cure_kind="complete_forward")
            summary["reconciled"] += 1
            _alert("mm_reconcile_completed",
                   f"buy {row_id} completed: sent {vsp_wei/WEI:.2f} VSP to {user}",
                   user=user, trade_id=row_id, tx=h)
            return
        else:  # refund
            # rev3: refund the fee only if its leg actually landed (buy legs run
            # principal -> fee -> payout; a fee leg that never executed means the
            # user never paid it, so refunding it would over-pay by the fee).
            refund_micro = int(r["reserves_micro"]) + (
                int(r["fee_micro"]) if r["leg_fee_in_tx"] else 0)
            if refund_micro / MICRO > MAX_USDC:
                _set_state(db, row_id, "failed", cure_kind="over_cap")
                summary["failed"] += 1
                _alert("mm_reconcile_over_cap",
                       f"buy {row_id} refund needs {refund_micro/MICRO:.2f} USDC > cap {MAX_USDC}",
                       user=user, trade_id=row_id)
                return
            mm_usdc = _balance_of(w3, USDC_ADDRESS, MM_ADDRESS)
            if mm_usdc < refund_micro:
                _note_open(db, r, "insolvent: MM lacks USDC to refund",
                           alert_kind="mm_reconcile_insolvent",
                           alert_msg=(f"buy {row_id}: MM lacks USDC to refund "
                                      f"({mm_usdc/MICRO:.2f} < {refund_micro/MICRO:.2f}); "
                                      f"top up MM hot USDC — will retry"),
                           user=user, trade_id=row_id)
                return
            from mm.erc20 import transfer
            h = transfer(USDC_ADDRESS, user, refund_micro)
            _set_state(db, row_id, "refunded", cure_tx=h, cure_kind="refund_back")
            summary["refunded"] += 1
            _alert("mm_reconcile_refunded",
                   f"buy {row_id} refunded: returned {refund_micro/MICRO:.2f} USDC to {user}",
                   user=user, trade_id=row_id, tx=h)
            return

    else:  # side == "sell"
        # principal_in = user VSP -> MM (landed). payout = MM USDC -> user (missing?)
        # Default: COMPLETE-forward the USDC payout (MM has the VSP, price is locked).
        if SELL_CURE == "refund":
            vsp_wei = int(r["vsp_wei"])
            if vsp_wei / WEI > MAX_VSP:
                _set_state(db, row_id, "failed", cure_kind="over_cap")
                summary["failed"] += 1
                return
            mm_vsp = _balance_of(w3, VSP_ADDRESS, MM_ADDRESS)
            if mm_vsp < vsp_wei:
                _note_open(db, r, "insolvent: MM lacks VSP to refund",
                           alert_kind="mm_reconcile_insolvent",
                           alert_msg=f"sell {row_id}: MM lacks VSP to refund — will retry",
                           user=user, trade_id=row_id)
                return
            from mm.erc20 import transfer
            h = transfer(VSP_ADDRESS, user, vsp_wei)
            _set_state(db, row_id, "refunded", cure_tx=h, cure_kind="refund_back")
            summary["refunded"] += 1
            _alert("mm_reconcile_refunded",
                   f"sell {row_id} refunded: returned {vsp_wei/WEI:.2f} VSP to {user}",
                   user=user, trade_id=row_id, tx=h)
            return
        else:  # complete
            payout_micro = int(r["reserves_micro"])
            if payout_micro / MICRO > MAX_USDC:
                _set_state(db, row_id, "failed", cure_kind="over_cap")
                summary["failed"] += 1
                _alert("mm_reconcile_over_cap",
                       f"sell {row_id} complete needs {payout_micro/MICRO:.2f} USDC > cap {MAX_USDC}",
                       user=user, trade_id=row_id)
                return
            mm_usdc = _balance_of(w3, USDC_ADDRESS, MM_ADDRESS)
            if mm_usdc < payout_micro:
                _note_open(db, r, "insolvent: MM lacks USDC to complete",
                           alert_kind="mm_reconcile_insolvent",
                           alert_msg=(f"sell {row_id}: MM lacks USDC to complete "
                                      f"({mm_usdc/MICRO:.2f} < {payout_micro/MICRO:.2f}); "
                                      f"top up MM hot USDC — will retry"),
                           user=user, trade_id=row_id)
                return
            from mm.erc20 import transfer
            h = transfer(USDC_ADDRESS, user, payout_micro)
            mark_leg(row_id, "leg_payout_out_tx", h)
            _set_state(db, row_id, "reconciled", cure_tx=h, cure_kind="complete_forward")
            summary["reconciled"] += 1
            _alert("mm_reconcile_completed",
                   f"sell {row_id} completed: sent {payout_micro/MICRO:.2f} USDC to {user}",
                   user=user, trade_id=row_id, tx=h)
            # rev3: the cure paid the USER; an expected fee leg (which runs
            # after the payout in the route) is still unsent — flag it.
            if int(r["fee_micro"]) > 0 and not r["leg_fee_in_tx"]:
                _alert("mm_reconcile_fee_leg_missing",
                       f"sell {row_id}: completed for the user; fee leg "
                       f"({int(r['fee_micro'])/MICRO:.2f} USDC -> sink) still unsent — settle manually",
                       user=user, trade_id=row_id)
            return
