# app/erc20.py
from web3 import Web3
from mm_wallet import w3, account, sign_and_send
from config import USDC_ADDRESS

# NOTE: POA middleware is already injected by mm_wallet.py on the shared w3 instance.
# Do NOT inject it again here — it causes "can't add the same name" errors.


def allowance(token_address: str, owner_address: str, spender_address: str) -> int:
    """Check ERC20 allowance."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=[
            {
                "constant": True,
                "inputs": [
                    {"name": "_owner", "type": "address"},
                    {"name": "_spender", "type": "address"}
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }
        ]
    )
    return contract.functions.allowance(
        Web3.to_checksum_address(owner_address),
        Web3.to_checksum_address(spender_address)
    ).call()


def transfer(token_address: str, to_address: str, amount_wei: int) -> str:
    """Transfer ERC20 tokens."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=[
            {
                "constant": False,
                "inputs": [
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]
    )

    tx = contract.functions.transfer(
        Web3.to_checksum_address(to_address),
        amount_wei
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
    })

    tx_hash = sign_and_send(tx)
    return tx_hash


def transfer_from(token_address: str, from_address: str, to_address: str, amount_wei: int) -> str:
    """Transfer ERC20 tokens from another address (requires approval)."""
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=[
            {
                "constant": False,
                "inputs": [
                    {"name": "_from", "type": "address"},
                    {"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}
                ],
                "name": "transferFrom",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            }
        ]
    )

    tx = contract.functions.transferFrom(
        Web3.to_checksum_address(from_address),
        Web3.to_checksum_address(to_address),
        amount_wei
    ).build_transaction({
        "from": account.address,
        "gas": 250000,
    })

    tx_hash = sign_and_send(tx)
    return tx_hash