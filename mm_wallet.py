# app/mm_wallet.py
# patch_bundle11_tx_signer_dedup: thin shim over the shared signer in tx_signer.py.
# Preserves the public API used across the app (w3, account, sign_and_send,
# TxRevertedError) with byte-for-byte-identical behavior: receipt timeout 60s and
# the 250_000 gas fallback (broadcast on estimate failure).
# patch_kms_mm: the MM key lives in GCP Cloud KMS (HSM); the private key never
# exists on this box. Fail-loud: MM_KMS_KEY + MM_ADDRESS required and must agree.
# MM_PRIVATE_KEY is deliberately ignored.
# patch_trackb_shared_w3: RPC list + build_w3 moved to chain.provider
from tx_signer import TxRevertedError, make_sign_and_send
from signing.kms_account import kms_account_from_env  # patch_kms_mm

from chain.provider import w3  # patch_trackb_shared_w3: shared handle owned outside the MM module

account = kms_account_from_env("MM")  # asserts MM_KMS_KEY derives MM_ADDRESS

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
