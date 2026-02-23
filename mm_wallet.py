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
    return w3.eth.send_raw_transaction(signed.raw_transaction).hex()