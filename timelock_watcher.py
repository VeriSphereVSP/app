#!/usr/bin/env python3
"""app/timelock_watcher.py — patch_bundle08_timelock_watcher

Bundle 8 safety net: poll the on-chain OpenZeppelin TimelockController for
governance events and fan them out to the alert sink (app/notify.py).
READ-ONLY against the chain — no signing, no on-chain writes; the only DB write
is the watcher's own block cursor.

Today (Fuji) governance is still the deployer EOA and the Timelock is empty, so
this watches an empty Timelock until the Bundle 12 ceremony moves governance
onto it. From that point on, every scheduled / executed / cancelled call and
every role-grant / role-revoke / min-delay change surfaces as an alert within
one poll cycle — the critical safety net called for in MVP-TASKLIST Bundle 8.

Cursor: persisted in the EXISTING `chain_indexer_state` key/value table under
key 'timelock_watch_last_block' (no new migration). Cold start anchors at
(safe_head - lookback) so a fresh DB does not replay all of chain history.

Wiring: a `_timelock_watcher()` asyncio task in worker.py calls poll_once() on
an interval, mirroring the idle-in-transaction monitor's task shape.
"""
import logging
import os

from web3 import Web3
from sqlalchemy import text as sql_text

from db import get_session_factory
from config import RPC_URL_READ, DEPLOYED

logger = logging.getLogger(__name__)

# ── Tunables (env-overridable; code defaults chosen so no env-file edit is
#    required to ship — same posture as the idle-tx monitor's thresholds). ──
TIMELOCK_WATCH_INTERVAL_SEC = int(os.getenv("TIMELOCK_WATCH_INTERVAL_SEC", "30"))
TIMELOCK_WATCH_BLOCK_BATCH = int(os.getenv("TIMELOCK_WATCH_BLOCK_BATCH", "2000"))
# Default to the indexer's confirmation depth so watcher and indexer agree on
# "safe head"; override independently if ever needed.
TIMELOCK_WATCH_CONFIRMATION_DEPTH = int(
    os.getenv("TIMELOCK_WATCH_CONFIRMATION_DEPTH",
              os.getenv("INDEXER_CONFIRMATION_DEPTH", "3"))
)
TIMELOCK_WATCH_COLD_LOOKBACK = int(os.getenv("TIMELOCK_WATCH_COLD_LOOKBACK", "100000"))

CURSOR_KEY = "timelock_watch_last_block"

# Resolved from app/deployments/<network>.json (same source config.py uses).
# Empty on a deployment without a Timelock entry -> watcher cleanly no-ops.
TIMELOCK_ADDRESS = (DEPLOYED.get("TimelockController", "") or "").strip()

# ── Minimal event-only ABI (OZ v5 TimelockController + AccessControl). Only the
#    events we decode, so we don't depend on the compiled artifact path. ──
_TIMELOCK_EVENT_ABI = [
    {"type": "event", "name": "CallScheduled", "anonymous": False, "inputs": [
        {"name": "id", "type": "bytes32", "indexed": True},
        {"name": "index", "type": "uint256", "indexed": True},
        {"name": "target", "type": "address", "indexed": False},
        {"name": "value", "type": "uint256", "indexed": False},
        {"name": "data", "type": "bytes", "indexed": False},
        {"name": "predecessor", "type": "bytes32", "indexed": False},
        {"name": "delay", "type": "uint256", "indexed": False},
    ]},
    {"type": "event", "name": "CallExecuted", "anonymous": False, "inputs": [
        {"name": "id", "type": "bytes32", "indexed": True},
        {"name": "index", "type": "uint256", "indexed": True},
        {"name": "target", "type": "address", "indexed": False},
        {"name": "value", "type": "uint256", "indexed": False},
        {"name": "data", "type": "bytes", "indexed": False},
    ]},
    {"type": "event", "name": "Cancelled", "anonymous": False, "inputs": [
        {"name": "id", "type": "bytes32", "indexed": True},
    ]},
    {"type": "event", "name": "MinDelayChange", "anonymous": False, "inputs": [
        {"name": "oldDuration", "type": "uint256", "indexed": False},
        {"name": "newDuration", "type": "uint256", "indexed": False},
    ]},
    {"type": "event", "name": "RoleGranted", "anonymous": False, "inputs": [
        {"name": "role", "type": "bytes32", "indexed": True},
        {"name": "account", "type": "address", "indexed": True},
        {"name": "sender", "type": "address", "indexed": True},
    ]},
    {"type": "event", "name": "RoleRevoked", "anonymous": False, "inputs": [
        {"name": "role", "type": "bytes32", "indexed": True},
        {"name": "account", "type": "address", "indexed": True},
        {"name": "sender", "type": "address", "indexed": True},
    ]},
]

# Events we poll, in attach order. CallScheduled is the headline ("new
# proposal"); the rest are governance-sensitive transitions worth surfacing.
_WATCHED_EVENTS = (
    "CallScheduled", "CallExecuted", "Cancelled",
    "MinDelayChange", "RoleGranted", "RoleRevoked",
)

# Alert `kind` per event, so downstream routing can prioritize scheduling/cancel
# over routine execution.
_EVENT_KIND = {
    "CallScheduled": "timelock_scheduled",
    "CallExecuted": "timelock_executed",
    "Cancelled": "timelock_cancelled",
    "MinDelayChange": "timelock_mindelay_change",
    "RoleGranted": "timelock_role_granted",
    "RoleRevoked": "timelock_role_revoked",
}

_w3 = None
_contract = None
_role_names = None


def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(RPC_URL_READ))
    return _w3


def _get_contract():
    global _contract
    if _contract is None:
        if not TIMELOCK_ADDRESS:
            return None
        w3 = _get_w3()
        _contract = w3.eth.contract(
            address=Web3.to_checksum_address(TIMELOCK_ADDRESS),
            abi=_TIMELOCK_EVENT_ABI,
        )
    return _contract


def _get_cursor(db) -> int:
    row = db.execute(sql_text(
        "SELECT value FROM chain_indexer_state WHERE key = :k"
    ), {"k": CURSOR_KEY}).fetchone()
    return int(row[0]) if row else 0


def _set_cursor(db, block: int) -> None:
    db.execute(sql_text("""
        INSERT INTO chain_indexer_state (key, value, updated_at)
        VALUES (:k, :v, now())
        ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
    """), {"k": CURSOR_KEY, "v": str(block)})


def _norm_hex(x) -> str:
    try:
        s = x.hex() if hasattr(x, "hex") else str(x)
    except Exception:
        s = str(x)
    s = s.lower()
    return s[2:] if s.startswith("0x") else s


def _hex0x(x) -> str:
    h = _norm_hex(x)
    return "0x" + h


def _short(s: str, n: int = 10) -> str:
    s = str(s)
    return s if len(s) <= 2 + n else (s[: 2 + n] + "\u2026")


def _role_name(role) -> str:
    """Best-effort map of a bytes32 role to its OZ name. Never raises."""
    global _role_names
    try:
        if _role_names is None:
            _role_names = {"00" * 32: "DEFAULT_ADMIN_ROLE"}
            for r in ("PROPOSER_ROLE", "EXECUTOR_ROLE", "CANCELLER_ROLE",
                      "TIMELOCK_ADMIN_ROLE"):
                _role_names[_norm_hex(Web3.keccak(text=r))] = r
        return _role_names.get(_norm_hex(role), _short(_hex0x(role)))
    except Exception:
        return _short(_hex0x(role))


def _describe_event(event_name: str, ev) -> str:
    a = ev["args"]
    if event_name == "CallScheduled":
        return (f"id={_short(_hex0x(a['id']))} index={a['index']} "
                f"target={a['target']} value={a['value']} delay={a['delay']}s "
                f"predecessor={_short(_hex0x(a['predecessor']))}")
    if event_name == "CallExecuted":
        return (f"id={_short(_hex0x(a['id']))} index={a['index']} "
                f"target={a['target']} value={a['value']}")
    if event_name == "Cancelled":
        return f"id={_short(_hex0x(a['id']))}"
    if event_name == "MinDelayChange":
        return f"old={a['oldDuration']}s new={a['newDuration']}s"
    if event_name in ("RoleGranted", "RoleRevoked"):
        return (f"role={_role_name(a['role'])} account={a['account']} "
                f"sender={a['sender']}")
    return str(a)


def poll_once() -> dict:
    """One read-only poll cycle. Returns a stats dict; never raises (the caller
    is a monitor loop). Sends one alert per new governance event in the
    freshly-confirmed block range, then advances the cursor. Alerts are
    best-effort (notify.send_alert never raises); the cursor only advances after
    the range is processed, so steady-state is exactly-once and a crash
    mid-cycle at worst re-alerts a small range (safe: better than missing one)."""
    contract = _get_contract()
    if contract is None:
        return {"skipped": "no_timelock_address"}

    w3 = _get_w3()
    try:
        head = int(w3.eth.block_number)
    except Exception as e:
        logger.warning("timelock-watch: head read failed: %s", e)
        return {"error": "head_read"}

    safe_head = head - TIMELOCK_WATCH_CONFIRMATION_DEPTH
    if safe_head < 0:
        return {"events": 0, "note": "chain_too_young"}

    Sess = get_session_factory()
    db = Sess()
    try:
        cursor = _get_cursor(db)
    finally:
        db.close()

    if cursor == 0:
        cursor = max(safe_head - TIMELOCK_WATCH_COLD_LOOKBACK, 0)
        logger.info("timelock-watch cold start: first poll from block %d "
                    "(head=%d, safe=%d, timelock=%s)",
                    cursor, head, safe_head, TIMELOCK_ADDRESS)

    from_block = cursor + 1
    to_block = min(from_block + TIMELOCK_WATCH_BLOCK_BATCH - 1, safe_head)
    if from_block > to_block:
        return {"events": 0, "from_block": from_block, "to_block": from_block - 1}

    # ── fetch (no DB session held across the RPC round-trips) ──
    collected = []
    try:
        for event_name in _WATCHED_EVENTS:
            event_obj = getattr(contract.events, event_name)
            for ev in event_obj.get_logs(from_block=from_block, to_block=to_block):
                collected.append((ev["blockNumber"], ev["logIndex"], event_name, ev))
    except Exception as e:
        logger.warning("timelock-watch: RPC fetch failed for %d-%d: %s",
                       from_block, to_block, e)
        return {"error": "rpc_fetch", "from_block": from_block, "to_block": to_block}

    collected.sort(key=lambda t: (t[0], t[1]))

    # ── alert (best-effort; never blocks the cursor) ──
    for block_no, log_index, event_name, ev in collected:
        detail = _describe_event(event_name, ev)
        kind = _EVENT_KIND.get(event_name, "timelock_event")
        txh = _hex0x(ev["transactionHash"])
        logger.warning("ALERT %s: block=%d tx=%s %s", kind, block_no, txh, detail)
        try:
            import notify
            notify.send_alert(
                kind,
                f"TimelockController {event_name} at block {block_no}",
                block=block_no, tx=txh, detail=detail, timelock=TIMELOCK_ADDRESS,
            )
        except Exception as e:
            logger.warning("timelock-watch: send_alert failed: %s", e)

    # ── advance cursor (own short transaction; never held across a sleep) ──
    db = Sess()
    try:
        _set_cursor(db, to_block)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("timelock-watch: cursor write failed (will retry range): %s", e)
        return {"error": "cursor_write", "events": len(collected),
                "from_block": from_block, "to_block": to_block}
    finally:
        db.close()

    return {"events": len(collected), "from_block": from_block, "to_block": to_block}


def describe() -> dict:
    """Read-only diagnostic for patch verification. Resolves the Timelock
    address, confirms RPC connectivity + head block + that code lives at the
    address, and reports the current cursor. Writes nothing; sends no alerts."""
    info = {
        "timelock": TIMELOCK_ADDRESS or None,
        "rpc": (RPC_URL_READ[:50] + "...") if RPC_URL_READ else None,
        "interval_sec": TIMELOCK_WATCH_INTERVAL_SEC,
        "confirmation_depth": TIMELOCK_WATCH_CONFIRMATION_DEPTH,
        "block_batch": TIMELOCK_WATCH_BLOCK_BATCH,
        "watched_events": list(_WATCHED_EVENTS),
    }
    if not TIMELOCK_ADDRESS:
        info["status"] = "NO_TIMELOCK_ADDRESS (watcher idle until one is deployed)"
        print("timelock-watch describe:", info)
        return info
    try:
        w3 = _get_w3()
        info["connected"] = bool(w3.is_connected())
        info["head_block"] = int(w3.eth.block_number)
        code = w3.eth.get_code(Web3.to_checksum_address(TIMELOCK_ADDRESS))
        info["code_present"] = bool(code) and code not in (b"", b"0x")
    except Exception as e:
        info["status"] = f"RPC_ERROR: {e}"
        print("timelock-watch describe:", info)
        return info
    try:
        Sess = get_session_factory()
        db = Sess()
        try:
            info["cursor"] = _get_cursor(db)
        finally:
            db.close()
    except Exception as e:
        info["cursor_error"] = str(e)
    info["status"] = "OK" if info.get("code_present") else "WARN_NO_CODE_AT_ADDRESS"
    print("timelock-watch describe:", info)
    return info
