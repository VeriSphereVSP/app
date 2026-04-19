# app/relay_guard.py
"""
Relay pre-flight guards.

These checks run BEFORE the relay spends any AVAX on gas. They catch
requests that would either fail on-chain or succeed without paying the
relay fee — both of which cost the relay operator money for nothing.

Design principle: every check here is a READ (eth_call or cached value).
None of them write state or cost gas. The relay only spends gas after
all guards pass.

Guards:
  1. Balance gate     — user must hold enough VSP for the operation
  2. Allowance gate   — user must have approved the relevant contract
  3. Fee viability    — user must be able to pay the relay fee
  4. Revert history   — addresses with high revert rates get throttled
  5. Minimum stake    — prevent dust-amount stakes that cost more gas than value
"""

import time
import logging
from collections import defaultdict
from web3 import Web3

from config import (
    FORWARDER_ADDRESS, VSP_ADDRESS, POST_REGISTRY_ADDRESS,
    STAKE_ENGINE_ADDRESS,
)
from mm_wallet import w3

logger = logging.getLogger(__name__)

# ── Function selectors ────────────────────────────────────────

SEL_CREATE_CLAIM = "4a3e1b89"
SEL_CREATE_LINK  = "ce919d33"
SEL_STAKE        = "7acb7757"
SEL_WITHDRAW     = "441a3e70"

# ── Minimal ABIs for view calls ───────────────────────────────

ERC20_BALANCE_ABI = [{
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]

ERC20_ALLOWANCE_ABI = [{
    "inputs": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
    ],
    "name": "allowance",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]

# ── Configurable thresholds ───────────────────────────────────

POSTING_FEE_WEI = 10**18           # 1 VSP — matches PostingFeePolicy default
MIN_STAKE_WEI = 10**16             # 0.01 VSP — below this, gas costs exceed value
REVERT_WINDOW = 600                # 10 minutes
MAX_REVERTS_PER_WINDOW = 5         # after 5 reverts in 10min, throttle
THROTTLE_COOLDOWN = 300            # 5 minute cooldown after throttle triggers

# ── Revert tracker ────────────────────────────────────────────

_revert_log: dict[str, list[float]] = defaultdict(list)
_throttled_until: dict[str, float] = {}


def record_revert(address: str):
    """Call this after a relay tx reverts on-chain."""
    addr = address.lower()
    now = time.time()
    _revert_log[addr].append(now)
    # Prune old entries
    cutoff = now - REVERT_WINDOW
    _revert_log[addr] = [t for t in _revert_log[addr] if t > cutoff]
    if len(_revert_log[addr]) >= MAX_REVERTS_PER_WINDOW:
        _throttled_until[addr] = now + THROTTLE_COOLDOWN
        logger.warning(
            "Address %s throttled: %d reverts in %ds",
            addr, len(_revert_log[addr]), REVERT_WINDOW,
        )


def _is_throttled(address: str) -> bool:
    addr = address.lower()
    until = _throttled_until.get(addr, 0)
    if time.time() < until:
        return True
    if addr in _throttled_until:
        del _throttled_until[addr]
    return False


# ── Balance/allowance cache ───────────────────────────────────
# Short TTL — just to avoid redundant RPC calls within a single
# request's guard checks.

_bal_cache: dict[str, tuple[int, float]] = {}
_BAL_TTL = 10  # seconds


def _get_vsp_balance(user: str) -> int:
    key = f"bal:{user.lower()}"
    now = time.time()
    if key in _bal_cache and now - _bal_cache[key][1] < _BAL_TTL:
        return _bal_cache[key][0]
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(VSP_ADDRESS),
        abi=ERC20_BALANCE_ABI,
    )
    bal = contract.functions.balanceOf(
        Web3.to_checksum_address(user)
    ).call()
    _bal_cache[key] = (bal, now)
    return bal


def _get_vsp_allowance(user: str, spender: str) -> int:
    key = f"allow:{user.lower()}:{spender.lower()}"
    now = time.time()
    if key in _bal_cache and now - _bal_cache[key][1] < _BAL_TTL:
        return _bal_cache[key][0]
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(VSP_ADDRESS),
        abi=ERC20_ALLOWANCE_ABI,
    )
    allow = contract.functions.allowance(
        Web3.to_checksum_address(user),
        Web3.to_checksum_address(spender),
    ).call()
    _bal_cache[key] = (allow, now)
    return allow


def invalidate_balance_cache(user: str):
    """Call after a successful permit or tx to bust the cache."""
    prefix = user.lower()
    stale = [k for k in _bal_cache if prefix in k]
    for k in stale:
        del _bal_cache[k]


# ── Main guard function ──────────────────────────────────────

class GuardError(Exception):
    """Raised when a pre-flight guard fails. Contains a user-friendly message."""
    def __init__(self, message: str, code: int = 400):
        self.message = message
        self.code = code
        super().__init__(message)


def check_relay_request(
    user_address: str,
    target_contract: str,
    calldata_hex: str,
    has_permit: bool = False,
    has_fee_permit: bool = False,
) -> None:
    """
    Run all pre-flight guards. Raises GuardError if any check fails.
    Call this BEFORE spending any gas.

    Args:
        user_address:    The meta-tx sender (request.from)
        target_contract: The target contract (request.to)
        calldata_hex:    The inner calldata (without 0x prefix)
        has_permit:      Whether a token permit is included in the request
        has_fee_permit:  Whether a fee permit is included in the request
    """
    user = user_address.lower()
    target = target_contract.lower()
    selector = calldata_hex[:8].lower() if len(calldata_hex) >= 8 else ""

    # ── Guard 1: Revert throttle ──────────────────────────────
    if _is_throttled(user):
        raise GuardError(
            "Too many failed transactions from this address. "
            "Please wait a few minutes before trying again.",
            429,
        )

    # ── Guard 2: VSP balance check ────────────────────────────
    balance = _get_vsp_balance(user_address)

    if selector == SEL_CREATE_CLAIM or selector == SEL_CREATE_LINK:
        # Needs posting fee (1 VSP)
        if balance < POSTING_FEE_WEI:
            raise GuardError(
                f"Insufficient VSP balance. You need at least 1 VSP to create "
                f"a claim. Your balance: {balance / 1e18:.4f} VSP."
            )

    elif selector == SEL_STAKE:
        # Decode stake amount from calldata
        if len(calldata_hex) >= 200:  # 8 + 64 + 64 + 64 = 200 hex chars
            stake_amount = int(calldata_hex[136:200], 16)
        else:
            stake_amount = 0

        if stake_amount == 0:
            raise GuardError("Stake amount cannot be zero.")

        if stake_amount < MIN_STAKE_WEI:
            raise GuardError(
                f"Stake amount too small. Minimum: {MIN_STAKE_WEI / 1e18:.4f} VSP."
            )

        if balance < stake_amount:
            raise GuardError(
                f"Insufficient VSP balance for this stake. "
                f"Requested: {stake_amount / 1e18:.4f} VSP, "
                f"balance: {balance / 1e18:.4f} VSP."
            )

    # ── Guard 3: Allowance check ──────────────────────────────
    # If no permit is provided, the user must already have approved
    # the target contract for the required amount.
    if not has_permit:
        if selector == SEL_CREATE_CLAIM or selector == SEL_CREATE_LINK:
            allowance = _get_vsp_allowance(user_address, target_contract)
            if allowance < POSTING_FEE_WEI:
                raise GuardError(
                    "Insufficient VSP allowance for the PostRegistry. "
                    "Please approve the contract or include a permit signature."
                )

        elif selector == SEL_STAKE:
            if len(calldata_hex) >= 200:
                stake_amount = int(calldata_hex[136:200], 16)
                allowance = _get_vsp_allowance(user_address, target_contract)
                if allowance < stake_amount:
                    raise GuardError(
                        "Insufficient VSP allowance for the StakeEngine. "
                        "Please approve the contract or include a permit signature."
                    )

    # ── Guard 4: Fee viability ────────────────────────────────
    # If fees are enabled and no fee permit is provided, check that
    # the user has already approved the Forwarder for the relay fee.
    # We don't enforce a specific fee amount here — just that there's
    # *some* allowance to the Forwarder, so the fee won't silently fail.
    if FORWARDER_ADDRESS and not has_fee_permit:
        forwarder_allowance = _get_vsp_allowance(user_address, FORWARDER_ADDRESS)
        if forwarder_allowance == 0:
            # Not fatal — the Forwarder's fee collection is non-reverting.
            # But log it so we can track fee evasion rates.
            logger.info(
                "Fee viability warning: %s has zero allowance to Forwarder. "
                "Relay fee will not be collected.",
                user_address[:10],
            )

    logger.debug(
        "Guards passed for %s: balance=%d selector=%s",
        user_address[:10], balance, selector,
    )
