# app/treasury_wallet.py
# patch_bundle11_tx_signer_dedup: thin shim over the shared signer in tx_signer.py.
#
# Signing module for the vsp-treasury-worker service. Hard-bound to the treasury
# worker's own EOA (MM_TREASURY_WORKER_PRIVATE_KEY), NOT the MM key. Per Bundle
# 10.5 decision #11 (fresh EOA): compromise of one key does not cascade to the
# other, and the worker key can be rotated independently of the MM trade key.
#
# Behavior preserved byte-for-byte vs the previous standalone implementation:
# receipt timeout 120s, and gas_estimate_fallback=None -> raise TxRevertedError on
# estimate failure (do NOT broadcast a doomed tx, e.g. mint over cap).
import logging
import os

from eth_account import Account
from config import RPC_WRITE_URLS  # patch_bundle10_rpc_failover
from tx_signer import TxRevertedError, build_w3, make_sign_and_send

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

w3 = build_w3(RPC_WRITE_URLS, conn_err_msg="treasury_wallet: Web3 RPC not connected")

if not MM_TREASURY_WORKER_PRIVATE_KEY:
    raise RuntimeError("treasury_wallet: MM_TREASURY_WORKER_PRIVATE_KEY not set")

account = Account.from_key(MM_TREASURY_WORKER_PRIVATE_KEY)

if account.address.lower() != EXPECTED_WORKER_ADDRESS.lower():
    raise RuntimeError(
        "treasury_wallet: MM_TREASURY_WORKER_PRIVATE_KEY derives %s, expected %s"
        % (account.address, EXPECTED_WORKER_ADDRESS)
    )

logger.info("treasury_wallet: signer ready, address=%s", account.address)

# receipt_timeout=120 + gas_estimate_fallback=None reproduce the original
# treasury_wallet.sign_and_send behavior exactly (raise on estimate failure,
# warning text "treasury_wallet.sign_and_send: receipt timeout ..." under logger
# name "treasury_wallet").
sign_and_send = make_sign_and_send(
    account=account,
    w3=w3,
    receipt_timeout=120,
    gas_estimate_fallback=None,
    logger=logger,
    label="treasury_wallet.sign_and_send",
)

__all__ = ["w3", "account", "sign_and_send", "TxRevertedError"]
