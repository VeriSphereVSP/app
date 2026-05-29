# app/treasury_wallet.py
# patch_bundle10_5_part2b_treasury_worker
#
# Signing module for the vsp-treasury-worker service. Parallel to mm_wallet.py
# but hard-bound to the treasury worker's own EOA (MM_TREASURY_WORKER_PRIVATE_KEY),
# NOT the MM key. Per Bundle 10.5 decision #11 (fresh EOA): compromise of one
# key does not cascade to the other, and the worker key can be rotated
# independently of the MM trade key.
#
# A Bundle 11 hygiene item will refactor sign_and_send to be address-agnostic so
# this module and mm_wallet.py can share one implementation. Until then this is
# an intentional near-duplicate (additive, lowest-risk for B10.5).

import logging
import os

from web3 import Web3
from eth_account import Account
from config import RPC_URL

logger = logging.getLogger(__name__)

# Expected worker EOA (Fuji). The startup assertion below refuses to run if the
# configured private key does not derive this address — a guard against pasting
# the wrong key into secrets.{network}.enc.yaml. For mainnet, this constant is
# updated to the mainnet worker EOA during the Bundle 12 ceremony.
EXPECTED_WORKER_ADDRESS = os.getenv(
    "MM_TREASURY_WORKER_ADDRESS",
    "0x3A6ECb7776070fd28F90331b7891fA645D699240",
)

MM_TREASURY_WORKER_PRIVATE_KEY = os.getenv("MM_TREASURY_WORKER_PRIVATE_KEY", "")

w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Inject POA middleware for Avalanche (web3.py v7+ renamed it). Same pattern as
# mm_wallet.py. This is a SEPARATE w3 instance from mm_wallet's, so injecting
# here does not collide with mm_wallet's injection.
try:
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
except ImportError:
    from web3.middleware import geth_poa_middleware
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not w3.is_connected():
    raise RuntimeError("treasury_wallet: Web3 RPC not connected")

if not MM_TREASURY_WORKER_PRIVATE_KEY:
    raise RuntimeError("treasury_wallet: MM_TREASURY_WORKER_PRIVATE_KEY not set")

account = Account.from_key(MM_TREASURY_WORKER_PRIVATE_KEY)

if account.address.lower() != EXPECTED_WORKER_ADDRESS.lower():
    raise RuntimeError(
        "treasury_wallet: MM_TREASURY_WORKER_PRIVATE_KEY derives %s, expected %s"
        % (account.address, EXPECTED_WORKER_ADDRESS)
    )

logger.info("treasury_wallet: signer ready, address=%s", account.address)


class TxRevertedError(Exception):
    """Raised by sign_and_send when the receipt status is 0 (on-chain revert).
    Carries tx_hash so the worker can alert with the hash. Mirrors
    mm_wallet.TxRevertedError."""

    def __init__(self, tx_hash: str, receipt=None, message: str = ""):
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(message or f"on-chain revert (tx_hash={tx_hash})")


def sign_and_send(tx: dict) -> str:
    """Sign and broadcast a transaction from the treasury worker EOA. Waits for
    the receipt and raises TxRevertedError on status==0. Returns the canonical
    0x-prefixed lowercase tx hash. Mirrors mm_wallet.sign_and_send semantics."""
    tx = dict(tx)
    tx.pop("gasPrice", None)

    try:
        base_fee = w3.eth.get_block("latest").baseFeePerGas
        priority = w3.eth.max_priority_fee * 150 // 100
        tx["type"] = 2
        tx["maxFeePerGas"] = base_fee + priority
        tx["maxPriorityFeePerGas"] = priority
    except Exception:
        tx["gasPrice"] = w3.eth.gas_price * 120 // 100

    tx["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
    tx["chainId"] = w3.eth.chain_id

    if "gas" not in tx:
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception:
            # estimate_gas reverts if the call itself would revert (e.g. mint
            # over cap). Surface that as a revert rather than guessing a gas
            # limit and broadcasting a doomed tx.
            raise TxRevertedError(
                "0x0",
                message="gas estimation reverted (call would revert pre-broadcast)",
            )

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    from web3.exceptions import TimeExhausted
    _raw = tx_hash.hex().lower().removeprefix("0x")
    _canonical = "0x" + _raw
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except TimeExhausted:
        logger.warning(
            "treasury_wallet.sign_and_send: receipt timeout for %s; returning hash anyway",
            _canonical,
        )
        return _canonical
    if getattr(receipt, "status", 1) == 0:
        raise TxRevertedError(_canonical, receipt=receipt)
    return _canonical
