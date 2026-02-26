# app/chain/chain_reader.py
"""
Read-only chain queries for stake totals, user stakes, and verity scores.
Used by claim-status endpoint and relay post-confirmation.
"""

import json
import logging
from pathlib import Path
from web3 import Web3
from config import STAKE_ENGINE_ADDRESS, SCORE_ENGINE_ADDRESS, RPC_URL

logger = logging.getLogger(__name__)

_w3 = None
_stake_engine = None
_score_engine = None


def _load_abi(name):
    path = Path(f"/core/out/{name}.sol/{name}.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)["abi"]
    return None


STAKE_ENGINE_ABI = _load_abi("StakeEngine") or [
    {
        "type": "function",
        "name": "getPostTotals",
        "inputs": [{"name": "postId", "type": "uint256"}],
        "outputs": [
            {"name": "support", "type": "uint256"},
            {"name": "challenge", "type": "uint256"},
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getUserStake",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "postId", "type": "uint256"},
            {"name": "side", "type": "uint8"},
        ],
        "outputs": [{"name": "total", "type": "uint256"}],
        "stateMutability": "view",
    },
]

SCORE_ENGINE_ABI = _load_abi("ScoreEngine") or [
    {
        "type": "function",
        "name": "effectiveVSRay",
        "inputs": [{"name": "postId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "int256"}],
        "stateMutability": "view",
    },
]


def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(RPC_URL))
    return _w3


def _get_stake_engine():
    global _stake_engine
    if _stake_engine is None:
        w3 = _get_w3()
        _stake_engine = w3.eth.contract(
            address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
            abi=STAKE_ENGINE_ABI,
        )
    return _stake_engine


def _get_score_engine():
    global _score_engine
    if _score_engine is None:
        w3 = _get_w3()
        _score_engine = w3.eth.contract(
            address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS),
            abi=SCORE_ENGINE_ABI,
        )
    return _score_engine


def get_stake_totals(post_id):
    """Returns (support_float, challenge_float) in VSP units."""
    try:
        se = _get_stake_engine()
        support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
        return support_wei / 1e18, challenge_wei / 1e18
    except Exception as e:
        logger.warning("Failed to read stake totals for post %d: %s", post_id, e)
        return 0.0, 0.0


def get_user_stake(user_address, post_id, side):
    """Returns user's stake in VSP units. side: 0=support, 1=challenge."""
    try:
        se = _get_stake_engine()
        addr = Web3.to_checksum_address(user_address)
        amount_wei = se.functions.getUserStake(addr, post_id, side).call()
        return amount_wei / 1e18
    except Exception as e:
        logger.warning(
            "Failed to read user stake for %s post %d side %d: %s",
            user_address, post_id, side, e,
        )
        return 0.0


def get_verity_score(post_id):
    """Returns verity score as a float in -100 to +100 range.
    effectiveVSRay returns a Ray-scaled int256 where 1e18 = 1.0 (i.e. 100%).
    Falls back to raw stake ratio if on-chain score is 0 but stakes exist."""
    try:
        se = _get_score_engine()
        vs_ray = se.functions.effectiveVSRay(post_id).call()
        vs = (vs_ray / 1e18) * 100
        if vs != 0:
            return vs
    except Exception as e:
        logger.warning("Failed to read verity score for post %d: %s", post_id, e)

    # Fallback: compute from raw stake totals
    # This covers the case where updatePost() hasn't been called yet
    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        if total > 0.001:
            return ((support - challenge) / total) * 100
    except Exception:
        pass

    return 0.0