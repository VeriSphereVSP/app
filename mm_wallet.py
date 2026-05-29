# app/mm_wallet.py
from web3 import Web3
from eth_account import Account
from config import RPC_URL, MM_PRIVATE_KEY, MM_ADDRESS

w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Inject POA middleware for Avalanche (web3.py v7+ renamed it)
try:
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
except ImportError:
    from web3.middleware import geth_poa_middleware
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

if not w3.is_connected():
    raise RuntimeError("Web3 RPC not connected")

if not MM_PRIVATE_KEY:
    raise RuntimeError("MM_PRIVATE_KEY not set")

account = Account.from_key(MM_PRIVATE_KEY)

if account.address.lower() != MM_ADDRESS.lower():
    raise RuntimeError("MM_PRIVATE_KEY does not match MM_ADDRESS")

# patch_bundle04_6_sign_and_send_strict
class TxRevertedError(Exception):
    """Raised by sign_and_send when wait_for_transaction_receipt returns
    status==0 (on-chain revert). Carries tx_hash so callers that need to
    record the submitted hash (e.g. /api/relay/async tx_log row) can do so.
    """
    def __init__(self, tx_hash: str, receipt=None, message: str = ""):
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(message or f"on-chain revert (tx_hash={tx_hash})")


def sign_and_send(tx: dict) -> str:
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
            tx["gas"] = 250_000

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    # patch_bundle04_6_sign_and_send_strict: strict revert handling.
    # Wait for receipt before returning so the caller's next tx sees the
    # updated chain state. Avoids race conditions on stake flips
    # (withdraw old side then stake new side).
    #
    # Three outcomes:
    #   - status == 1     → success: fall through and return the canonical hash.
    #   - status == 0     → on-chain revert: raise TxRevertedError carrying the hash.
    #   - timeout         → caller may still want the hash (e.g. relay records it
    #                       and lets the unified resolve_pending_txs watcher catch
    #                       up); log a warning but do not raise.
    from web3.exceptions import TimeExhausted
    _raw = tx_hash.hex().lower().removeprefix("0x")
    _canonical = "0x" + _raw
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    except TimeExhausted:
        import logging
        logging.getLogger(__name__).warning(
            "sign_and_send: receipt timeout for %s; returning hash anyway",
            _canonical,
        )
        return _canonical
    if getattr(receipt, "status", 1) == 0:
        raise TxRevertedError(_canonical, receipt=receipt)
    return _canonical
