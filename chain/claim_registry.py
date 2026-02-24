# app/chain/claim_registry.py
from web3 import Web3
from .abi import POST_REGISTRY_ABI
from mm_wallet import w3, account, sign_and_send
from config import POST_REGISTRY_ADDRESS

def create_claim(text: str) -> str:
    if not POST_REGISTRY_ADDRESS:
        raise ValueError("POST_REGISTRY_ADDRESS not set")
    
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=POST_REGISTRY_ABI
    )

    try:
        # Build transaction - this will estimate gas and might fail
        tx = contract.functions.createClaim(text).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "gas": 300000,  # Set explicit gas to skip estimation
        })
    except Exception as e:
        print(f"Error building transaction: {e}")
        raise

    tx_hash = sign_and_send(tx)
    return tx_hash

def create_link(independent_post_id: int, dependent_post_id: int, is_challenge: bool) -> str:
    if not POST_REGISTRY_ADDRESS:
        raise ValueError("POST_REGISTRY_ADDRESS not set")
    
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=POST_REGISTRY_ABI
    )
    
    try:
        tx = contract.functions.createLink(
            independent_post_id,
            dependent_post_id,
            is_challenge
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "gas": 400000,  # Set explicit gas
        })
    except Exception as e:
        print(f"Error building link transaction: {e}")
        raise
    
    tx_hash = sign_and_send(tx)
    return tx_hash