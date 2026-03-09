# app/chain/check_duplicate.py
"""
Check if a claim already exists on-chain by doing a static call to createClaim.
If it reverts with DuplicateClaim(existingPostId), the claim exists.
This is O(1) — uses the contract's internal hash mapping, no iteration.
"""

import logging
from web3 import Web3
from config import POST_REGISTRY_ADDRESS, RPC_URL

logger = logging.getLogger(__name__)

DUPLICATE_CLAIM_SELECTOR = "c314bc02"

_CREATE_CLAIM_ABI = [
    {
        "type": "function",
        "name": "createClaim",
        "inputs": [{"name": "text_", "type": "string"}],
        "outputs": [{"name": "postId", "type": "uint256"}],
        "stateMutability": "nonpayable",
    }
]

_w3 = None
_contract = None


def _get_contract():
    global _w3, _contract
    if _contract is None:
        _w3 = Web3(Web3.HTTPProvider(RPC_URL))
        _contract = _w3.eth.contract(
            address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
            abi=_CREATE_CLAIM_ABI,
        )
    return _w3, _contract


def check_claim_exists_onchain(text: str, from_address: str = None) -> dict | None:
    """
    Returns {"post_id": int} if the claim already exists on-chain, None otherwise.
    Uses a static call to createClaim — if DuplicateClaim(postId) is thrown,
    extracts the existing post_id from the revert data.
    """
    if not POST_REGISTRY_ADDRESS or not RPC_URL:
        return None

    try:
        w3, contract = _get_contract()
        caller = from_address or "0x0000000000000000000000000000000000000001"

        try:
            # Static call — will revert with DuplicateClaim if it exists
            contract.functions.createClaim(text).call({
                "from": Web3.to_checksum_address(caller),
            })
            # If we get here, the claim does NOT exist yet
            return None
        except Exception as call_err:
            post_id = _extract_duplicate_post_id(call_err)
            if post_id is not None:
                logger.info(
                    "DuplicateClaim detected: text='%s' existing post_id=%d",
                    text[:50], post_id)
                return {"post_id": post_id}
            # Some other revert (InvalidClaim, InsufficientAllowance, etc.)
            return None

    except Exception as e:
        logger.warning("check_claim_exists_onchain failed: %s", e)
        return None


def _extract_duplicate_post_id(err) -> int | None:
    """Extract post_id from a DuplicateClaim revert error."""
    # Method 1: e.data attribute (web3.py ContractCustomError)
    if hasattr(err, 'data') and isinstance(err.data, str):
        hex_data = err.data.removeprefix("0x")
        if hex_data.startswith(DUPLICATE_CLAIM_SELECTOR) and len(hex_data) >= 72:
            return int(hex_data[8:72], 16)

    # Method 2: parse from error message or args
    for source in [str(err)] + [str(a) for a in getattr(err, 'args', [])]:
        if DUPLICATE_CLAIM_SELECTOR in source:
            idx = source.find(DUPLICATE_CLAIM_SELECTOR)
            candidate = source[idx:]
            cleaned = ""
            for c in candidate:
                if c in "0123456789abcdefABCDEF":
                    cleaned += c
                else:
                    break
            if len(cleaned) >= 72:
                return int(cleaned[8:72], 16)

    return None