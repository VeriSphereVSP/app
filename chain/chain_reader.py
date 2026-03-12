# app/chain/chain_reader.py
"""
On-chain read helpers.

StakeEngine v2 view functions (getUserStake, getPostTotals) already
project epoch gains/losses lazily — they always return the current
virtual balance, not stale snapshots. No separate projection needed.
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
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "sMax",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "numTranches",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getUserLotInfo",
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "postId", "type": "uint256"},
            {"name": "side", "type": "uint8"},
        ],
        "outputs": [
            {"name": "amount", "type": "uint256"},
            {"name": "weightedPosition", "type": "uint256"},
            {"name": "entryEpoch", "type": "uint256"},
            {"name": "sideTotal", "type": "uint256"},
            {"name": "tranche", "type": "uint256"},
            {"name": "positionWeight", "type": "uint256"},
        ],
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
    """Returns (support, challenge) in VSP units. Already projected."""
    try:
        se = _get_stake_engine()
        support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
        return support_wei / 1e18, challenge_wei / 1e18
    except Exception as e:
        logger.warning("Failed to read stake totals for post %d: %s", post_id, e)
        return 0.0, 0.0


def get_user_stake(user_address, post_id, side):
    """Returns user's projected stake in VSP units. side: 0=support, 1=challenge."""
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
    Falls back to stake-share formula if on-chain score is 0 but stakes exist.
    
    Formula: if support > challenge → +(support/total)*100
             if challenge > support → -(challenge/total)*100
             if equal or zero → 0
    """
    try:
        se = _get_score_engine()
        vs_ray = se.functions.effectiveVSRay(post_id).call()
        vs = (vs_ray / 1e18) * 100
        return vs  # 0 is a valid score (contested/neutral)
    except Exception as e:
        logger.warning("Failed to read verity score for post %d: %s", post_id, e)

    # Fallback: compute using same formula as ScoreEngine.baseVSRay
    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        if total > 0.001:
            if support > challenge:
                return (support / total) * 100
            elif challenge > support:
                return -(challenge / total) * 100
            else:
                return 0.0
    except Exception:
        pass

    return 0.0


def get_estimated_apr(post_id, side="support"):
    """Estimate annualized rate for a position on this post.
    
    Formula from whitepaper:
      rEff = rMin + (rMax - rMin) * v * participation
    where:
      v = abs(VS) / 100  (truth pressure, 0-1)
      participation = T / sMax  (post size factor, 0-1)
      rMin = 1% APR, rMax = 100% APR (from StakeRatePolicy)
    
    Winners (side matches VS sign): earn at +rEff APR (newly minted VSP)
    Losers (side opposes VS sign): lose at -rEff APR (stake burned)
    """
    R_MIN = 0.01  # 1% APR minimum
    R_MAX = 1.00  # 100% APR maximum
    
    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        if total < 0.001:
            return 0.0
        
        vs = get_verity_score(post_id)
        abs_vs = abs(vs)
        v = abs_vs / 100.0  # normalized truth pressure (0-1)
        
        # Get sMax from contract
        try:
            se = _get_stake_engine()
            s_max_wei = se.functions.sMax().call()
            s_max = s_max_wei / 1e18
        except Exception:
            s_max = total  # fallback: assume this post IS the max
        
        if s_max < 0.001:
            s_max = total
        
        participation = min(total / s_max, 1.0)  # post size factor (0-1)
        
        # Effective annual rate
        r_eff = R_MIN + (R_MAX - R_MIN) * v * participation
        
        # Determine if this side wins or loses
        support_wins = vs > 0
        is_winner = (side == "support" and support_wins) or (side == "challenge" and not support_wins)
        
        if vs == 0:
            return 0.0  # no pressure at VS=0
        
        return r_eff * 100 if is_winner else -r_eff * 100  # return as percentage
        
    except Exception as e:
        logger.warning("Failed to estimate APR for post %d: %s", post_id, e)
        return 0.0


def get_apr_breakdown(post_id, side="support"):
    """Return APR and its component factors for display.
    
    Returns dict with:
      apr: final APR percentage
      r_min, r_max: rate bounds
      vs: verity score
      abs_vs: absolute VS
      v: truth pressure (0-1)
      total_stake: post total stake
      s_max: system-wide max stake
      participation: post size factor (0-1)
      r_eff: effective rate before sign
      is_winner: whether this side is winning
    """
    R_MIN = 0.01
    R_MAX = 1.00
    
    result = {
        "apr": 0.0, "r_min": R_MIN * 100, "r_max": R_MAX * 100,
        "vs": 0.0, "abs_vs": 0.0, "v": 0.0,
        "total_stake": 0.0, "s_max": 0.0, "participation": 0.0,
        "r_eff": 0.0, "is_winner": False,
    }
    
    try:
        support, challenge = get_stake_totals(post_id)
        total = support + challenge
        result["total_stake"] = round(total, 4)
        
        if total < 0.001:
            return result
        
        vs = get_verity_score(post_id)
        abs_vs = abs(vs)
        v = abs_vs / 100.0
        result["vs"] = round(vs, 2)
        result["abs_vs"] = round(abs_vs, 2)
        result["v"] = round(v, 4)
        
        try:
            se = _get_stake_engine()
            s_max_wei = se.functions.sMax().call()
            s_max = s_max_wei / 1e18
        except Exception:
            s_max = total
        
        if s_max < 0.001:
            s_max = total
        result["s_max"] = round(s_max, 4)
        
        participation = min(total / s_max, 1.0)
        result["participation"] = round(participation, 4)
        
        r_eff = R_MIN + (R_MAX - R_MIN) * v * participation
        result["r_eff"] = round(r_eff * 100, 2)
        
        support_wins = vs > 0
        is_winner = (side == "support" and support_wins) or (side == "challenge" and not support_wins)
        result["is_winner"] = is_winner
        
        if vs == 0:
            return result
        
        result["apr"] = round(r_eff * 100 if is_winner else -r_eff * 100, 1)
        return result
        
    except Exception as e:
        logger.warning("Failed to get APR breakdown for post %d: %s", post_id, e)
        return result


def get_user_lot_info(user_address, post_id, side):
    """Returns lot info: (amount, weightedPosition, entryEpoch, sideTotal, tranche, positionWeight).
    positionWeight is RAY-scaled (1e18 = 1.0 = best position)."""
    try:
        se = _get_stake_engine()
        addr = Web3.to_checksum_address(user_address)
        result = se.functions.getUserLotInfo(addr, post_id, side).call()
        return {
            "amount": result[0] / 1e18,
            "weighted_position": result[1] / 1e18,
            "entry_epoch": result[2],
            "side_total": result[3] / 1e18,
            "tranche": result[4],
            "position_weight": result[5] / 1e18,  # 1.0 = best, 0.1 = worst
        }
    except Exception as e:
        logger.warning("Failed to get lot info for %s post %d side %d: %s",
                       user_address, post_id, side, e)
        return None
