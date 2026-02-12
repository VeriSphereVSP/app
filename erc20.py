from web3 import Web3
from mm_wallet import w3, account, sign_and_send

ERC20_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "transferFrom",
        "type": "function",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
    },
]


def contract(address):
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)


def allowance(token, owner, spender) -> int:
    return contract(token).functions.allowance(owner, spender).call()


def transfer(token, to, amount):
    tx = contract(token).functions.transfer(
        to, int(amount)
    ).build_transaction({"from": account.address})
    return sign_and_send(tx)


def transfer_from(token, src, dst, amount):
    tx = contract(token).functions.transferFrom(
        src, dst, int(amount)
    ).build_transaction({"from": account.address})
    return sign_and_send(tx)

