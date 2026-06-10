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

RELAY_PRIVATE_KEY = os.getenv("RELAY_PRIVATE_KEY", "")
RELAY_ADDRESS = os.getenv("RELAY_ADDRESS", "")

w3 = build_w3(RPC_WRITE_URLS, conn_err_msg="relay_wallet: Web3 RPC not connected")

if not RELAY_PRIVATE_KEY:
    raise RuntimeError(
        "relay_wallet: RELAY_PRIVATE_KEY not set. Relay/MM key separation (#298) is "
        "required and fail-loud: provision RELAY_PRIVATE_KEY in "
        "env/secrets.<network>.enc.yaml (and fund the relay EOA with gas) before start. "
        "There is deliberately no fallback to the MM key."
    )
if not RELAY_ADDRESS:
    raise RuntimeError(
        "relay_wallet: RELAY_ADDRESS not set. It is required so startup can assert the "
        "private key derives the expected relay EOA (guards against a mismatched paste)."
    )

account = Account.from_key(RELAY_PRIVATE_KEY)

if account.address.lower() != RELAY_ADDRESS.lower():
    raise RuntimeError(
        "relay_wallet: RELAY_PRIVATE_KEY derives %s, expected RELAY_ADDRESS %s"
        % (account.address, RELAY_ADDRESS)
    )

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
