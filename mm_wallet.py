# app/mm_wallet.py
# patch_bundle11_tx_signer_dedup: thin shim over the shared signer in tx_signer.py.
# Preserves the public API used across the app (w3, account, sign_and_send,
# TxRevertedError) with byte-for-byte-identical behavior: receipt timeout 60s and
# the 250_000 gas fallback (broadcast on estimate failure).
from eth_account import Account
from config import RPC_WRITE_URLS, MM_PRIVATE_KEY, MM_ADDRESS  # patch_bundle10_rpc_failover
from tx_signer import TxRevertedError, build_w3, make_sign_and_send

w3 = build_w3(RPC_WRITE_URLS)

if not MM_PRIVATE_KEY:
    raise RuntimeError("MM_PRIVATE_KEY not set")

account = Account.from_key(MM_PRIVATE_KEY)

if account.address.lower() != MM_ADDRESS.lower():
    raise RuntimeError("MM_PRIVATE_KEY does not match MM_ADDRESS")

# receipt_timeout=60 + gas_estimate_fallback=250_000 reproduce the original
# mm_wallet.sign_and_send behavior exactly. label/logger preserve the original
# warning text ("sign_and_send: receipt timeout ...") under logger name "mm_wallet".
import logging as _logging
sign_and_send = make_sign_and_send(
    account=account,
    w3=w3,
    receipt_timeout=60,
    gas_estimate_fallback=250_000,
    logger=_logging.getLogger(__name__),
    label="sign_and_send",
)

__all__ = ["w3", "account", "sign_and_send", "TxRevertedError"]
