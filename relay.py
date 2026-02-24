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
from mm_wallet import w3, sign_and_send

logger = logging.getLogger(__name__)
router = APIRouter()

RECEIPT_TIMEOUT = 30


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


class RelayRequest(BaseModel):
    request: ForwardRequestPayload
    signature: str


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
    from chain.chain_reader import get_stake_totals, get_user_stake
    support, challenge = get_stake_totals(post_id)
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
        result["user_support"] = get_user_stake(user_address, post_id, 0)
        result["user_challenge"] = get_user_stake(user_address, post_id, 1)
    return result


@router.get("/api/relay/nonce/{address}")
async def get_nonce(address: str) -> NonceResponse:
    try:
        fwd = _get_forwarder()
        nonce = fwd.functions.nonces(Web3.to_checksum_address(address)).call()
        return NonceResponse(nonce=nonce)
    except Exception as e:
        logger.exception("Failed to get nonce")
        raise HTTPException(500, str(e))


@router.post("/api/relay")
async def relay(body: RelayRequest, db: Session = Depends(get_db)):
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

        # Submit transaction
        tx = fwd.functions.execute(request_data).build_transaction({
            "from": w3.eth.default_account or Web3.to_checksum_address(
                __import__("config").MM_ADDRESS),
            "value": req.value,
            "gas": req.gas + 80_000,
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
            raise HTTPException(400,
                "Transaction reverted on-chain. "
                "Common causes: insufficient VSP balance, duplicate claim, or contract error.")

        logger.info("Meta-tx confirmed: tx=%s gasUsed=%d", tx_hash, receipt.gasUsed)

        response = {"ok": True, "tx_hash": tx_hash}

        # Detect createClaim
        is_create = (
            req.to.lower() == POST_REGISTRY_ADDRESS.lower()
            and calldata_hex[:8] == CREATE_CLAIM_SELECTOR
        )

        if is_create:
            try:
                reg = _get_post_registry()
                logs = reg.events.PostCreated().process_receipt(receipt, errors=DISCARD)
                if logs:
                    post_id = logs[0].args.postId
                    claim_text = _decode_claim_text(calldata_hex)
                    _mark_claim_on_chain(db, claim_text, post_id)
                    claim_state = _get_claim_state(post_id, req.from_)
                    claim_state["text"] = claim_text
                    claim_state["creator"] = req.from_
                    response["claim"] = claim_state
                    logger.info("Claim created: post_id=%d text=%s", post_id, claim_text[:50])
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
            except Exception as e:
                logger.warning("Post-stake processing failed (non-fatal): %s", e)

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Relay failed")
        raise HTTPException(500, str(e))