# app/app/mm_wallet.py
from web3 import Web3
from eth_account import Account
from .config import RPC_URL, MM_PRIVATE_KEY, MM_ADDRESS

w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    raise RuntimeError("Web3 RPC not connected")

if not MM_PRIVATE_KEY:
    raise RuntimeError("MM_PRIVATE_KEY not set")

account = Account.from_key(MM_PRIVATE_KEY)

if account.address.lower() != MM_ADDRESS.lower():
    raise RuntimeError("MM_PRIVATE_KEY does not match MM_ADDRESS")

def sign_and_send(tx):
    tx = tx.copy()

    # Remove legacy gasPrice to force EIP-1559
    tx.pop("gasPrice", None)

    # Get fee data
    try:
        fee_data = w3.eth.fee_history(5, "latest", [25])
        base_fee = fee_data['baseFeePerGas'][-1]
        priority_fee = w3.eth.max_priority_fee

        # Bump priority fee by 50% for replacements/speed
        priority_fee = priority_fee * 150 // 100
        max_fee = base_fee + priority_fee

        tx["type"] = 2
        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = priority_fee
    except Exception as e:
        print("EIP-1559 fee fetch failed:", e)
        # Fallback to legacy gasPrice
        tx["gasPrice"] = w3.eth.gas_price * 120 // 100  # 20% bump

    # Nonce & chain
    tx["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
    tx["chainId"] = w3.eth.chain_id

    # Gas estimation
    if "gas" not in tx:
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception as e:
            print("Gas estimation failed:", e)
            tx["gas"] = 200_000  # safe default

    print("Signing tx:", tx)

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()
