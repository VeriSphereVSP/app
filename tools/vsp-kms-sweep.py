#!/usr/bin/env python3
"""vsp-kms-sweep.py — emergency MM hot->cold USDC sweep, signed via GCP KMS.

Run INSIDE the app container (where the MM KMS signer and env live):
    docker exec -i verisphere-app-1 python /app/tools/vsp-kms-sweep.py [amount_micro]

No private key: signs with kms_account_from_env("MM"). If amount is omitted, sweeps
the MM's entire USDC balance. Destination is VSP_COLD_RESERVE_ADDRESS. Prints the tx
hash and receipt status; exits nonzero on failure so the caller can react.
"""
import os, sys
sys.path.insert(0, '/app')  # so the 'signing' package resolves when run by path
from web3 import Web3
from signing.kms_account import kms_account_from_env

ERC20 = [
    {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"v","type":"uint256"}],"name":"transfer",
     "outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
]

def main() -> int:
    rpc = os.environ["RPC_URL"].split(",")[0]
    w3 = Web3(Web3.HTTPProvider(rpc))
    usdc = w3.eth.contract(address=Web3.to_checksum_address(os.environ["USDC_ADDRESS"]), abi=ERC20)
    cold = Web3.to_checksum_address(os.environ["VSP_COLD_RESERVE_ADDRESS"])

    mm = kms_account_from_env("MM")   # asserts MM_KMS_KEY derives MM_ADDRESS
    bal = usdc.functions.balanceOf(mm.address).call()
    amt = int(sys.argv[1]) if len(sys.argv) > 1 else bal
    if amt <= 0:
        print(f"[kms-sweep] MM USDC balance is {bal/1e6:.6f}; nothing to sweep."); return 0
    if amt > bal:
        print(f"[kms-sweep] requested {amt/1e6:.6f} > balance {bal/1e6:.6f}; clamping."); amt = bal

    print(f"[kms-sweep] MM {mm.address} hot USDC {bal/1e6:.6f} -> sweeping {amt/1e6:.6f} to cold {cold}")
    tx = usdc.functions.transfer(cold, amt).build_transaction({
        "from": mm.address,
        "nonce": w3.eth.get_transaction_count(mm.address, "pending"),
        "gas": 90_000,
        "gasPrice": w3.eth.gas_price,
    })
    h = w3.eth.send_raw_transaction(mm.sign_transaction(tx).raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
    print(f"[kms-sweep] tx={h.hex()} status={rcpt.status}")
    return 0 if rcpt.status == 1 else 8

if __name__ == "__main__":
    sys.exit(main())
