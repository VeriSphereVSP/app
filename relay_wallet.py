# app/relay_wallet.py
# patch_bundle10_relay_key_separation (MVP-TASKLIST #298: separate MM key from relay key).
#
# Dedicated signer for the relay / meta-tx gas-payer path. Hard-bound to its own
# EOA (RELAY_PRIVATE_KEY), NOT the MM key. The relay relays user meta-txns through
# the Forwarder and PAYS THE GAS; before this split it signed with the MM key,
# which also custodies the USDC reserves — so a relay-path compromise reached the
# reserves. The relay EOA holds only gas AVAX, so that blast radius is now closed
# (risk R4/R5).
#
# Why no on-chain / fee changes are needed:
#   * VerisphereForwarder extends OpenZeppelin ERC2771Forwarder; execute() is
#     permissionless (it verifies the user's EIP-712 signature, not the relayer's),
#     so an arbitrary relay EOA is authorized with no contract call.
#   * The relay fee is pulled to the Forwarder's `treasury` (safeTransferFrom(user,
#     treasury, fee)), independent of who relays — so the fee still lands where it did.
#   * Targets see the user via _msgSender(), never the relayer.
#
# Option A (operator decision): FAIL LOUD. If RELAY_PRIVATE_KEY is unset, RELAY_ADDRESS
# is unset, or they don't match, the app refuses to start — no silent fallback to the
# MM key (which would defeat the separation). Provision RELAY_PRIVATE_KEY + RELAY_ADDRESS
# in env/secrets.<network>.enc.yaml and fund the EOA with gas before cutover.
import logging
import os

from eth_account import Account
from config import RPC_WRITE_URLS  # patch_bundle10_rpc_failover
from tx_signer import TxRevertedError, build_w3, make_sign_and_send

logger = logging.getLogger(__name__)

# patch_kms_relay: the relay key now lives in GCP Cloud KMS (HSM). The private key
# never exists on this box; signing happens via asymmetricSign. Fail-loud policy is
# unchanged: RELAY_KMS_KEY + RELAY_ADDRESS are required and must agree, with NO
# fallback to a raw private key (RELAY_PRIVATE_KEY is deliberately ignored).
RELAY_ADDRESS = os.getenv("RELAY_ADDRESS", "")

w3 = build_w3(RPC_WRITE_URLS, conn_err_msg="relay_wallet: Web3 RPC not connected")

from signing.kms_account import kms_account_from_env  # patch_kms_relay

account = kms_account_from_env("RELAY")  # asserts RELAY_KMS_KEY derives RELAY_ADDRESS

logger.info("relay_wallet: signer ready, address=%s", account.address)

# receipt_timeout=60 + gas_estimate_fallback=250_000 mirror the mm profile. The
# fallback is effectively moot here: every relay tx sets `gas` explicitly
# (execute() = req.gas + 800_000; permit() = 120_000), so estimate_gas is never
# called — but mirroring mm keeps one consistent broadcast-and-let-the-pipeline-
# catch-reverts policy (relay.py already catches TxRevertedError to record tx_log).
sign_and_send = make_sign_and_send(
    account=account,
    w3=w3,
    receipt_timeout=60,
    gas_estimate_fallback=250_000,
    logger=logger,
    label="relay_wallet.sign_and_send",
)

__all__ = ["w3", "account", "sign_and_send", "TxRevertedError"]
