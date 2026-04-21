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
from chain_indexer import trigger_reindex
from web3.logs import DISCARD

from config import FORWARDER_ADDRESS, POST_REGISTRY_ADDRESS
from db import get_db
from mm_wallet import w3, sign_and_send
from moderation import check_content
from rate_limit import relay_rate_limit

logger = logging.getLogger(__name__)
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
_post_registry = None


def _get_forwarder():
    global _forwarder
    if _forwarder is None:
        if not FORWARDER_ADDRESS:
            raise HTTPException(500, "Forwarder address not configured")
        _forwarder = w3.eth.contract(
            address=Web3.to_checksum_address(FORWARDER_ADDRESS), abi=FORWARDER_ABI)
    return _forwarder


def _get_post_registry():
    global _post_registry
    if _post_registry is None:
        _post_registry = w3.eth.contract(
            address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=POST_REGISTRY_ABI)
    return _post_registry


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

    tx = contract.functions.permit(
        owner_addr, spender_addr, value, permit.deadline, permit.v, r_bytes, s_bytes,
    ).build_transaction({"from": mm_addr, "gas": 120_000})

    tx_hash = sign_and_send(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if receipt.status == 0:
        raise HTTPException(400, "Permit transaction reverted")
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


@router.post("/api/relay")
@relay_rate_limit
async def relay(body: RelayRequest, db: Session = Depends(get_db)):
    try:
        fwd = _get_forwarder()
        req = body.request
        print(f"RELAY DEBUG: req.gas={req.gas} req.to={req.to[:10]}")
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

        # Execute permit if provided (relay pays gas)
        if body.permit:
            _execute_permit(body.permit)

        # Execute fee permit if provided (grants Forwarder VSP allowance for relay fee)
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

        # Check if this is a createClaim call
        is_create = (
            req.to.lower() == POST_REGISTRY_ADDRESS.lower()
            and calldata_hex[:8] == CREATE_CLAIM_SELECTOR
        )

        # Pre-flight: for createClaim, check for duplicate BEFORE wasting gas
        if is_create:
            dup = _check_duplicate_claim(calldata_hex, req.from_, db)
            if dup:
                logger.info("Pre-flight: claim already exists on-chain, returning existing")
                return dup

        # Pre-flight: simulate inner call to catch reverts cheaply
        # Skip for createClaim (already handled by duplicate-check pre-flight above)
        if not is_create:
            try:
                w3.eth.call({
                    "from": Web3.to_checksum_address(req.from_),
                    "to": Web3.to_checksum_address(req.to),
                    "data": "0x" + calldata_hex,
                    "value": req.value,
                })
            except Exception as sim_err:
                reason = _decode_revert_reason(sim_err)
                logger.info("Pre-flight simulation reverted: %s", reason)
                raise HTTPException(400, reason)

        # Submit transaction
        tx = fwd.functions.execute(request_data).build_transaction({
            "from": w3.eth.default_account or Web3.to_checksum_address(
                __import__("config").MM_ADDRESS),
            "value": req.value,
            "gas": req.gas + 800_000,
        })
        tx_hash = sign_and_send(tx)
        logger.info("Submitted meta-tx: from=%s to=%s tx=%s", req.from_, req.to, tx_hash)

        # Wait for receipt
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
        except Exception as e:
            logger.warning("Receipt timeout: %s", e)
            raise HTTPException(500,
                f"Transaction submitted ({tx_hash}) but could not confirm. "
                "Please check the transaction on the explorer.")

        if receipt.status == 0:
            logger.warning("Meta-tx REVERTED: tx=%s gasUsed=%d", tx_hash, receipt.gasUsed)

            # For createClaim reverts, try DuplicateClaim recovery
            if is_create:
                dup = _check_duplicate_claim(calldata_hex, req.from_, db)
                if dup:
                    dup["tx_hash"] = tx_hash
                    return dup

            raise HTTPException(400,
                "Transaction reverted on-chain. "
                "Common causes: insufficient VSP balance, duplicate claim, or contract error.")

        logger.info("Meta-tx confirmed: tx=%s gasUsed=%d", tx_hash, receipt.gasUsed)

        response = {"ok": True, "tx_hash": tx_hash}

        # Detect createClaim success
        if is_create:
            try:
                reg = _get_post_registry()
                logs = reg.events.PostCreated().process_receipt(receipt, errors=DISCARD)
                if logs:
                    post_id = logs[0].args.postId
                    claim_text = _decode_claim_text(calldata_hex)
                    _mark_claim_on_chain(db, claim_text, post_id)
                    # Incremental cache update: link this new claim to any article
                    # sentence with matching text
                    try:
                        from articles.article_store import apply_new_post
                        apply_new_post(db, post_id, claim_text)
                    except Exception as e:
                        logger.debug("apply_new_post failed (non-fatal): %s", e)
                    claim_state = _get_claim_state(post_id, req.from_)
                    claim_state["text"] = claim_text
                    claim_state["creator"] = req.from_
                    response["claim"] = claim_state
                    logger.info("Claim created: post_id=%d text=%s", post_id, claim_text[:50])
                    # APP-07: Bust chain_reader cache
                    try:
                        from chain.chain_reader import _cache as _cr_cache
                        for _ck in list(_cr_cache.keys()):
                            if f":{post_id}" in _ck:
                                del _cr_cache[_ck]
                    except Exception:
                        pass

                    # Immediate cross-index into all relevant articles
                    try:
                        from articles.claim_indexer import cross_index_claim_into_all_articles
                        from chain_indexer import _queue_article_refresh
                        cross_index_claim_into_all_articles(db, claim_text, post_id)
                        _queue_article_refresh(db, post_id)
                    except Exception as e:
                        logger.debug("Cross-index from relay failed (non-fatal): %s", e)

                    # APP-10: Universal topic association
                    try:
                        from articles.topic_detect import detect_topic, ensure_article_for_claim
                        _topic = detect_topic(claim_text)
                        if _topic:
                            from sqlalchemy import text as _sqlt
                            db.execute(_sqlt(
                                "UPDATE claim SET topic = :t WHERE post_id = :pid AND topic IS NULL"
                            ), {"t": _topic, "pid": post_id})
                            db.commit()
                            ensure_article_for_claim(db, claim_text, post_id, _topic)
                            logger.info("Auto-topic: post_id=%d → '%s'", post_id, _topic)
                    except Exception as e:
                        logger.debug("Auto-topic failed (non-fatal): %s", e)
                    # APP-11: Synchronous cache rebuild so frontend sees update immediately
                    try:
                        from db import get_session_factory
                        from articles.article_store import build_and_cache_response
                        # Find which articles contain this post
                        _art_rows = db.execute(__import__("sqlalchemy").text(
                            "SELECT DISTINCT ta.topic_key FROM article_sentence s "
                            "JOIN article_section sec ON s.section_id = sec.section_id "
                            "JOIN topic_article ta ON sec.article_id = ta.article_id "
                            "WHERE s.post_id = :pid"
                        ), {"pid": post_id}).fetchall()
                        for _ar in _art_rows:
                            build_and_cache_response(get_session_factory(), _ar[0])
                        if _art_rows:
                            logger.info("Cache rebuilt for %d articles after claim create", len(_art_rows))
                    except Exception as e:
                        logger.debug("Sync cache rebuild failed (non-fatal): %s", e)

                else:
                    logger.warning("createClaim succeeded but no PostCreated event found")
            except Exception as e:
                logger.warning("Post-create processing failed (non-fatal): %s", e)

        # Detect stake/withdraw (target is StakeEngine)
        from config import STAKE_ENGINE_ADDRESS
        is_stake = req.to.lower() == STAKE_ENGINE_ADDRESS.lower()

        if is_stake:
            try:
                data = bytes.fromhex(calldata_hex)
                post_id = int.from_bytes(data[4:36], "big")
                claim_state = _get_claim_state(post_id, req.from_)
                response["claim"] = claim_state
                logger.info("Stake updated: post_id=%d", post_id)
                # APP-07: Bust chain_reader cache for this post
                try:
                    from chain.chain_reader import _cache as _cr_cache
                    for _ck in list(_cr_cache.keys()):
                        if f":{post_id}" in _ck:
                            del _cr_cache[_ck]
                except Exception:
                    pass
                # Re-index this post and connected posts so VS/stakes are fresh
                try:
                    from chain_indexer import index_post, _reindex_connected, _queue_article_refresh
                    index_post(db, post_id)
                    _reindex_connected(db, post_id)
                    _queue_article_refresh(db, post_id)
                except Exception as e2:
                    logger.debug("Post-stake reindex failed (non-fatal): %s", e2)
                # APP-11: Rebuild article caches after stake change
                try:
                    from db import get_session_factory
                    from articles.article_store import build_and_cache_response
                    _art_rows = db.execute(__import__("sqlalchemy").text(
                        "SELECT DISTINCT ta.topic_key FROM article_sentence s "
                        "JOIN article_section sec ON s.section_id = sec.section_id "
                        "JOIN topic_article ta ON sec.article_id = ta.article_id "
                        "WHERE s.post_id = :pid"
                    ), {"pid": post_id}).fetchall()
                    for _ar in _art_rows:
                        build_and_cache_response(get_session_factory(), _ar[0])
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Post-stake processing failed (non-fatal): %s", e)

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Relay failed")
        raise HTTPException(500, str(e))