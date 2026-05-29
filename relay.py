# app/relay.py
"""
Gasless meta-transaction relay.
Pattern: submit tx -> wait for receipt -> update DB -> return authoritative state.
"""

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from web3 import Web3
from web3.logs import DISCARD

from config import FORWARDER_ADDRESS, POST_REGISTRY_ADDRESS
from db import get_db
from mm_wallet import w3, sign_and_send, TxRevertedError
from fee_calculator import compute_relay_fee, is_fee_exempt
from config import VSP_ADDRESS, MM_ADDRESS as FEE_WALLET
from moderation import check_content
from rate_limit import relay_rate_limit

logger = logging.getLogger(__name__)

# patch_bundle04b2: /api/relay (sync) + _relay_sync + _get_post_registry
# + trigger_reindex import removed. Frontend now calls /api/relay/async
# exclusively (since bundle 4b-1). The synchronous post-success path
# (receipt wait, reindex, cache busts) was duplicated work — the
# indexer's normal poll cycle handles all of it.

# VSP token ABI for fee collection
_VSP_FEE_ABI = [
    {"inputs":[{"name":"from","type":"address"},{"name":"to","type":"address"},
               {"name":"value","type":"uint256"}],
     "name":"transferFrom","outputs":[{"type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "name":"allowance","outputs":[{"type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],
     "name":"balanceOf","outputs":[{"type":"uint256"}],
     "stateMutability":"view","type":"function"},
]

def _detect_tx_type(calldata_hex, to_addr):
    """Detect transaction type and value from calldata."""
    sel = calldata_hex[:8]
    tx_type = "unknown"
    tx_value_vsp = 0
    if sel == "84c08ed3":  # createClaim(string)
        tx_type = "claim"
    elif sel == "b6f0b787":  # createLink(uint256,uint256,bool)
        tx_type = "link"
    elif sel == "f99b7d75":  # stake(uint256,uint8,uint256) — legacy
        tx_type = "stake"
        try:
            from eth_abi import decode
            _, _, amt = decode(["uint256","uint8","uint256"], bytes.fromhex(calldata_hex[8:]))
            tx_value_vsp = amt / 1e18
        except: pass
    elif sel == "b1cf8aac":  # setStake(uint256,int256) — current
        tx_type = "stake"
        try:
            from eth_abi import decode
            _, target = decode(["uint256","int256"], bytes.fromhex(calldata_hex[8:]))
            # target is signed; absolute value for telemetry
            tx_value_vsp = abs(target) / 1e18
        except: pass
    elif sel == "97be5523" or sel == "441a3e70":  # withdraw variants
        tx_type = "unstake"
        try:
            from eth_abi import decode
            _, _, amt, _ = decode(["uint256","uint8","uint256","bool"], bytes.fromhex(calldata_hex[8:]))
            tx_value_vsp = amt / 1e18
        except: pass
    elif sel == "095ea7b3":  # approve
        tx_type = "approve"
    elif sel == "a9059cbb":  # transfer
        tx_type = "transfer"
        try:
            from eth_abi import decode
            _, amt = decode(["address","uint256"], bytes.fromhex(calldata_hex[8:]))
            tx_value_vsp = amt / 1e18
        except: pass
    return tx_type, tx_value_vsp

# Known custom errors — selector → human message
_KNOWN_ERRORS = {
    "cbca5aa2": "Amount cannot be zero",
    "0dfa289a": "Invalid side (must be 0 or 1)",
    "f0a42d4c": "Not enough stake to withdraw",
    "546dcceb": "Cannot stake on opposite side — you already have a stake on the other side of this claim",
    "33cb1ab6": "Post is not active",
    "7e81c055": "Invalid post ID",
    "b00d4d75": "This link already exists",
    "c314bc02": "Claim already exists",
    "49b39990": "Cannot link a claim to itself",
    "7ad1f845": "Source post does not exist",
    "22fa5e05": "Target post does not exist",
    "7861979c": "Source post must be a claim",
    "2b3d067e": "Target post must be a claim",
    "bd73f403": "Claim text is too long",
    "fb8f41b2": "Insufficient VSP allowance",
    "e450d38c": "Insufficient VSP balance",
    "d6bda275": "Transaction failed — likely insufficient balance or contract rejection",
}


def _decode_revert_reason(err) -> str:
    """Extract a human-readable revert reason from a web3 call exception."""
    import re
    s = str(err)
    m = re.search(r"0x([0-9a-fA-F]{8})", s)
    if m:
        sel = m.group(1).lower()
        if sel in _KNOWN_ERRORS:
            return _KNOWN_ERRORS[sel]
    if "0x11" in s or "underflow" in s.lower():
        return "Contract arithmetic error (this is a bug — please report)"
    m2 = re.search(r"execution reverted: ?([^\n,\"]+)", s)
    if m2:
        return f"Reverted: {m2.group(1).strip()}"
    return "Transaction would fail on-chain. Common causes: insufficient VSP balance, duplicate action, or contract constraint."


router = APIRouter()

RECEIPT_TIMEOUT = 30

# Error selectors
DUPLICATE_CLAIM_SELECTOR = "c314bc02"


def _load_abi(name):
    path = Path(f"/core/out/{name}.sol/{name}.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)["abi"]
    return None


FORWARDER_ABI = _load_abi("VerisphereForwarder") or [
    {"inputs":[{"components":[
        {"name":"from","type":"address"},{"name":"to","type":"address"},
        {"name":"value","type":"uint256"},{"name":"gas","type":"uint256"},
        {"name":"deadline","type":"uint48"},{"name":"data","type":"bytes"},
        {"name":"signature","type":"bytes"}
    ],"name":"request","type":"tuple"}],
    "name":"execute","outputs":[],"stateMutability":"payable","type":"function"},
    {"inputs":[{"components":[
        {"name":"from","type":"address"},{"name":"to","type":"address"},
        {"name":"value","type":"uint256"},{"name":"gas","type":"uint256"},
        {"name":"deadline","type":"uint48"},{"name":"data","type":"bytes"},
        {"name":"signature","type":"bytes"}
    ],"name":"request","type":"tuple"}],
    "name":"verify","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"}],"name":"nonces",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"innerData","type":"bytes"}],"name":"estimateFee",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

POST_REGISTRY_ABI = _load_abi("PostRegistry") or [
    {"type":"event","name":"PostCreated","anonymous":False,"inputs":[
        {"name":"postId","type":"uint256","indexed":True},
        {"name":"creator","type":"address","indexed":True},
        {"name":"contentType","type":"uint8","indexed":False}
    ]},
]

# Function selectors (first 4 bytes of keccak256)
CREATE_CLAIM_SELECTOR = "4a3e1b89"


class ForwardRequestPayload(BaseModel):
    model_config = {"populate_by_name": True}
    from_: str = Field(alias="from")
    to: str
    value: int
    gas: int
    nonce: int
    deadline: int
    data: str


class PermitPayload(BaseModel):
    token: str
    owner: str
    spender: str
    value: str  # String to handle large numbers
    deadline: int
    v: int
    r: str
    s: str


class RelayRequest(BaseModel):
    request: ForwardRequestPayload
    signature: str
    permit: PermitPayload | None = None
    fee_permit: PermitPayload | None = None  # Permit granting Forwarder VSP allowance for relay fee


class NonceResponse(BaseModel):
    nonce: int


_forwarder = None


def _get_forwarder():
    global _forwarder
    if _forwarder is None:
        if not FORWARDER_ADDRESS:
            raise HTTPException(500, "Forwarder address not configured")
        _forwarder = w3.eth.contract(
            address=Web3.to_checksum_address(FORWARDER_ADDRESS), abi=FORWARDER_ABI)
    return _forwarder


def _decode_claim_text(calldata_hex):
    """Decode claim text from createClaim(string) calldata."""
    data = bytes.fromhex(calldata_hex)
    offset = int.from_bytes(data[4:36], "big")
    str_start = 4 + offset
    str_len = int.from_bytes(data[str_start:str_start + 32], "big")
    return data[str_start + 32:str_start + 32 + str_len].decode("utf-8")


def _mark_claim_on_chain(db, claim_text, post_id):
    from semantic import ensure_claim, get_post_id
    from sqlalchemy import text as sql_text
    cid = ensure_claim(db, claim_text)
    existing = get_post_id(db, cid)
    if existing is None:
        db.execute(sql_text(
            "UPDATE claim SET post_id = :pid WHERE claim_id = :cid"
        ), {"pid": post_id, "cid": cid})
        db.commit()
        logger.info("Marked claim on-chain: claim_id=%d post_id=%d", cid, post_id)


def _get_claim_state(post_id, user_address=None):
    from db import get_session_factory
    from chain.chain_db import get_stake_totals, get_user_stake
    _db = get_session_factory()()
    try:
        support, challenge = get_stake_totals(_db, post_id)
        result = {
            "post_id": post_id,
            "text": "",
            "creator": "",
            "support_total": support,
            "challenge_total": challenge,
            "user_support": 0,
            "user_challenge": 0,
        }
        if user_address:
            try:
                result["user_support"] = get_user_stake(_db, user_address, post_id, 0)
                result["user_challenge"] = get_user_stake(_db, user_address, post_id, 1)
            except Exception:
                pass
        return result
    finally:
        _db.close()


def _check_duplicate_claim(calldata_hex, req_from, db):
    """
    Do a static call to createClaim. If it reverts with DuplicateClaim(postId),
    recover the existing post_id and return a success response.
    """
    try:
        claim_text = _decode_claim_text(calldata_hex)
        reg_address = Web3.to_checksum_address(POST_REGISTRY_ADDRESS)
        try:
            w3.eth.call({
                "to": reg_address,
                "from": Web3.to_checksum_address(req_from),
                "data": "0x" + calldata_hex,
            })
            return None
        except Exception as call_err:
            err_data = ""
            if hasattr(call_err, 'data') and isinstance(call_err.data, str):
                err_data = call_err.data.removeprefix("0x")
            elif hasattr(call_err, 'args') and call_err.args:
                for arg in call_err.args:
                    s = str(arg)
                    if DUPLICATE_CLAIM_SELECTOR in s:
                        idx = s.find(DUPLICATE_CLAIM_SELECTOR)
                        err_data = s[idx:]
                        cleaned = ""
                        for c in err_data:
                            if c in "0123456789abcdefABCDEF":
                                cleaned += c
                            else:
                                break
                        err_data = cleaned
                        break

            if not err_data or DUPLICATE_CLAIM_SELECTOR not in err_data:
                err_str = str(call_err)
                if DUPLICATE_CLAIM_SELECTOR in err_str:
                    idx = err_str.find(DUPLICATE_CLAIM_SELECTOR)
                    err_data = err_str[idx:]
                    cleaned = ""
                    for c in err_data:
                        if c in "0123456789abcdefABCDEF":
                            cleaned += c
                        else:
                            break
                    err_data = cleaned

            if err_data.startswith(DUPLICATE_CLAIM_SELECTOR) and len(err_data) >= 72:
                post_id = int(err_data[8:72], 16)
                logger.info(
                    "DuplicateClaim detected: text='%s' existing post_id=%d",
                    claim_text[:50], post_id)
                _mark_claim_on_chain(db, claim_text, post_id)
                claim_state = _get_claim_state(post_id, req_from)
                claim_state["text"] = claim_text
                claim_state["creator"] = req_from
                return {
                    "ok": True,
                    "tx_hash": None,
                    "duplicate": True,
                    "claim": claim_state,
                }
    except Exception as e:
        logger.warning("DuplicateClaim check failed: %s", e)
    return None





def _execute_permit(permit):
    """Execute an EIP-2612 permit on behalf of the user. Relay pays gas."""
    token_addr = Web3.to_checksum_address(permit.token)
    owner_addr = Web3.to_checksum_address(permit.owner)
    spender_addr = Web3.to_checksum_address(permit.spender)
    value = int(permit.value)
    r_bytes = bytes.fromhex(permit.r.removeprefix("0x"))
    s_bytes = bytes.fromhex(permit.s.removeprefix("0x"))
    permit_abi = [{
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "name": "permit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }]
    contract = w3.eth.contract(address=token_addr, abi=permit_abi)
    mm_addr = Web3.to_checksum_address(__import__("config").MM_ADDRESS)

    # Debug: log permit details
    logger.info("Executing permit: token=%s owner=%s spender=%s value=%d deadline=%d v=%d",
        token_addr[:10], owner_addr[:10], spender_addr[:10], value, permit.deadline, permit.v)

    # Static call to catch revert reason before spending gas
    try:
        contract.functions.permit(
            owner_addr, spender_addr, value, permit.deadline, permit.v, r_bytes, s_bytes,
        ).call({"from": mm_addr})
    except Exception as static_err:
        logger.warning("Permit static call failed: %s", static_err)
        try:
            nonce_data = w3.eth.call({"to": token_addr,
                "data": "0x7ecebe00" + encode(["address"], [owner_addr]).hex()})
            logger.warning("  On-chain permit nonce: %d", int.from_bytes(nonce_data, "big"))
        except:
            pass
        raise HTTPException(400, f"Permit would revert: {str(static_err)[:300]}")

    tx = contract.functions.permit(
        owner_addr, spender_addr, value, permit.deadline, permit.v, r_bytes, s_bytes,
    ).build_transaction({"from": mm_addr, "gas": 120_000})

    tx_hash = sign_and_send(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if receipt.status == 0:
        raise HTTPException(400, "Permit transaction reverted on-chain")
    logger.info("Permit executed: token=%s owner=%s spender=%s tx=%s",
                permit.token[:10], permit.owner[:10], permit.spender[:10], tx_hash)


def _moderate_claim(calldata_hex: str) -> None:
    """Check if createClaim calldata contains blocked content. Raises HTTPException if blocked."""
    try:
        selector = calldata_hex[:8]
        if selector.lower() != CREATE_CLAIM_SELECTOR:
            return  # Not a createClaim call, skip moderation
        claim_text = _decode_claim_text(calldata_hex)
        result = check_content(claim_text)
        if not result.allowed:
            raise HTTPException(400, f"Content blocked: {result.reason}")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Moderation decode failed (allowing): {e}")


@router.get("/api/relay/nonce/{address}")
async def get_nonce(address: str):
    try:
        fwd = _get_forwarder()
        nonce = fwd.functions.nonces(Web3.to_checksum_address(address)).call()
        return NonceResponse(nonce=nonce)
    except Exception as e:
        logger.exception("Failed to get nonce")
        raise HTTPException(500, str(e))



@router.get("/api/relay/estimate-fee")
async def estimate_fee(to: str, calldata: str, db: Session = Depends(get_db)):
    """Estimate the relay fee for a given transaction.
    Used by the frontend to show users the fee before signing."""
    try:
        fwd = _get_forwarder()
        calldata_bytes = bytes.fromhex(calldata.removeprefix("0x"))
        # Call estimateFee on the forwarder contract
        fee_wei = fwd.functions.estimateFee(calldata_bytes).call()
        fee_vsp = fee_wei / 1e18
        # Get VSP price for USD display
        try:
            from fee_calculator import _get_vsp_price_usd
            vsp_price = _get_vsp_price_usd(db)
        except Exception:
            vsp_price = 1.30
        return {
            "fee_vsp": round(fee_vsp, 6),
            "fee_usd": round(fee_vsp * vsp_price, 4),
            "fee_wei": str(fee_wei),
            "vsp_price_usd": vsp_price,
        }
    except Exception as e:
        raise HTTPException(500, f"Fee estimation failed: {e}")

# ── /api/relay/async (Bundle 4a) ────────────────────────────────────
# Submits a meta-transaction and returns immediately with tx_hash and a
# tx_log row id. The chain indexer's poll cycle resolves the row by
# fetching the receipt later, and the frontend polls
# /api/notifications/{address} to learn the outcome.
#
# Code is DUPLICATED from /api/relay's _relay_sync rather than shared via a
# helper. The two paths will diverge during the transition: /api/relay keeps
# all its post-submit work (receipt wait, reindex, cache busts), this one
# does none of that. After the frontend cuts over and /api/relay is
# deleted, the duplication goes away.
#
# NOT replicated from /api/relay: the TIMING: prints, DuplicateClaim
# recovery on revert, post-success state attachment in the response.
# Those were UX optimizations for synchronous responses that no longer
# apply in the async flow.

@router.post("/api/relay/async")
@relay_rate_limit
async def relay_async(body: RelayRequest, db: Session = Depends(get_db)):
    """Async relay: submit tx, record tx_log row, return immediately."""
    import asyncio
    return await asyncio.to_thread(_relay_async_sync, body, db)


def _relay_async_sync(body: RelayRequest, db: Session):
    from tx_log import record_pending
    try:
        fwd = _get_forwarder()
        req = body.request
        sig_bytes = bytes.fromhex(body.signature.removeprefix("0x"))
        calldata_hex = req.data.removeprefix("0x")

        request_data = (
            Web3.to_checksum_address(req.from_),
            Web3.to_checksum_address(req.to),
            req.value,
            req.gas,
            req.deadline,
            bytes.fromhex(calldata_hex),
            sig_bytes,
        )

        # Permits (gasless pre-grant of allowances)
        if body.permit:
            _execute_permit(body.permit)

        if body.fee_permit:
            try:
                _execute_permit(body.fee_permit)
                logger.info("Fee permit executed for %s", req.from_[:10])
            except Exception as e:
                logger.debug("Fee permit skip (non-fatal): %s", e)

        # Content moderation gate
        _moderate_claim(calldata_hex)

        # Verify signature
        try:
            is_valid = fwd.functions.verify(request_data).call()
            if not is_valid:
                raise HTTPException(400, "Invalid signature")
        except HTTPException:
            raise
        except Exception as e:
            if "invalid" in str(e).lower() or "revert" in str(e).lower():
                raise HTTPException(400, f"Signature verification failed: {e}")
            logger.warning("verify() call failed (proceeding anyway): %s", e)

        # createClaim detection — pre-flight duplicate check
        is_create = (
            req.to.lower() == POST_REGISTRY_ADDRESS.lower()
            and calldata_hex[:8] == CREATE_CLAIM_SELECTOR
        )
        if is_create:
            dup = _check_duplicate_claim(calldata_hex, req.from_, db)
            if dup:
                logger.info("Pre-flight: claim already exists on-chain, returning existing")
                # In async mode the frontend wasn't waiting for a result. Return
                # the duplicate marker; client can route accordingly.
                return {"status": "duplicate_claim", "claim": dup.get("claim")}

        # Pre-flight simulation for non-create calls
        if not is_create:
            try:
                w3.eth.call({
                    "from": Web3.to_checksum_address(req.from_),
                    "to":   Web3.to_checksum_address(req.to),
                    "data": "0x" + calldata_hex,
                    "value": req.value,
                })
            except Exception as sim_err:
                reason = _decode_revert_reason(sim_err)
                logger.info("Pre-flight simulation reverted: %s", reason)
                raise HTTPException(400, reason)

        # On-chain fee verification (read what forwarder will charge)
        user_addr = Web3.to_checksum_address(req.from_)
        try:
            calldata_bytes = bytes.fromhex(calldata_hex)
            fee_wei = fwd.functions.estimateFee(calldata_bytes).call()
        except Exception as e:
            raise HTTPException(400, f'Forwarder rejects this call: {e}')

        vsp_c = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_ADDRESS), abi=_VSP_FEE_ABI)
        user_balance = vsp_c.functions.balanceOf(user_addr).call()
        user_allowance = vsp_c.functions.allowance(
            user_addr, Web3.to_checksum_address(FORWARDER_ADDRESS)).call()
        if user_balance < fee_wei:
            raise HTTPException(400,
                f'Insufficient VSP for relay fee: need {fee_wei / 1e18:.4f} VSP, '
                f'have {user_balance / 1e18:.4f}')
        if user_allowance < fee_wei:
            raise HTTPException(400,
                f'Insufficient VSP allowance for relay fee: need {fee_wei / 1e18:.4f} VSP, '
                f'allowance {user_allowance / 1e18:.4f}. Sign a fee permit.')

        # Build, sign, submit
        tx = fwd.functions.execute(request_data).build_transaction({
            "from": w3.eth.default_account or Web3.to_checksum_address(
                __import__("config").MM_ADDRESS),
            "value": req.value,
            "gas":   req.gas + 800_000,
        })
        # patch_bundle04_6_relay_revert_catch: catch on-chain revert so tx_log still records the hash.
        # The unified resolve_pending_txs / chain_indexer pipeline will see the
        # failed receipt and surface it via the verisphere:tx-confirmed FE event;
        # the only thing we need to do here is make sure the row exists.
        try:
            tx_hash = sign_and_send(tx)
            tx_status = "submitted"
        except TxRevertedError as e:
            tx_hash = e.tx_hash
            tx_status = "reverted"
            logger.warning(
                "Async relay tx reverted on-chain (from=%s to=%s tx=%s); "
                "recording tx_log row so unified pipeline can surface failure",
                req.from_, req.to, tx_hash,
            )
        logger.info("Async relay submitted: from=%s to=%s tx=%s status=%s",
                    req.from_, req.to, tx_hash, tx_status)

        # Record pending tx_log row (resolve_pending_txs will move it to confirmed/failed).
        action_type, action_value = _detect_tx_type(calldata_hex, req.to)
        tx_log_id = record_pending(
            db,
            tx_hash=tx_hash,
            user_address=req.from_,
            to_address=req.to,
            calldata=calldata_hex,
            action_type=action_type,
            action_value=action_value,
        )
        db.commit()

        return {
            "tx_hash":      tx_hash,
            "tx_log_id":    tx_log_id,
            "action_type":  action_type,
            "action_value": action_value,
            "status":       tx_status,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Async relay failed")
        raise HTTPException(500, f"Relay error: {e}")
