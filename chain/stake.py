# app/chain/stake.py
from web3 import Web3
from .abi import STAKE_ENGINE_ABI
from mm_wallet import w3, account, sign_and_send
from config import STAKE_ENGINE_ADDRESS

def stake_claim(claim_id: int, side: str, amount: int) -> str:
    if not STAKE_ENGINE_ADDRESS:
        raise ValueError("STAKE_ENGINE_ADDRESS not set")
    
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=STAKE_ENGINE_ABI
    )
    side_code = 0 if side == "support" else 1
    
    tx = contract.functions.stake(claim_id, side_code, amount).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "gas": 350000,  # Explicit gas
    })
    tx_hash = sign_and_send(tx)
    return tx_hash

def withdraw_stake(claim_id: int, side: str, amount: int, lifo: bool = True) -> str:
    if not STAKE_ENGINE_ADDRESS:
        raise ValueError("STAKE_ENGINE_ADDRESS not set")
    
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=STAKE_ENGINE_ABI
    )
    
    side_code = 0 if side == "support" else 1
    amount_wei = amount * 10**18
    
    tx = contract.functions.withdraw(
        claim_id,
        side_code,
        amount_wei,
        lifo
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "gas": 350000,  # Explicit gas
    })
    
    tx_hash = sign_and_send(tx)
    return tx_hash