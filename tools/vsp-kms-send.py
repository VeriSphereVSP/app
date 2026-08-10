#!/usr/bin/env python3
"""vsp-kms-send.py — send or drain native AVAX / ERC-20 from a KMS-backed wallet.

The three operational wallets (relay, MM, treasury-worker) are KMS-backed: there is
NO private key to import into MetaMask, so they can only be moved by KMS-signing.
This tool makes "drain any KMS wallet" a one-liner, satisfying the operational
requirement that every funded account must always be drainable.

Runs INSIDE the app container (needs the signing package + KMS identity via the
metadata server). Example:

    docker exec -i verisphere-app-1 python /app/tools/vsp-kms-send.py \
        --from MM --asset native --to cold --amount ALL --execute

Safety model:
  * DRY-RUN BY DEFAULT. Nothing broadcasts without --execute.
  * Address book: send to a name ('cold', 'fee', 'deployer'…) not a raw hex string.
  * Fail-loud: the resolved recipient is printed and must be a valid checksummed
    address; a typo'd name aborts rather than guessing.
  * ALL: for native, sends balance − (gasPrice × gasLimit × safety); for ERC-20,
    sends the full token balance. uint256 math done in Python (never bash).
  * Confirmation: --execute additionally requires typing the recipient to confirm,
    unless --yes is passed (for scripted drains).
"""
import argparse, os, sys, json

# --- make the app's packages importable whether run as /app/tools/... or cwd ---
for p in ("/app", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
    if p not in sys.path:
        sys.path.insert(0, p)

def die(msg, code=2):
    print(f"[kms-send] FATAL: {msg}", file=sys.stderr)
    sys.exit(code)

# --- address book -------------------------------------------------------------
# Names resolve to addresses. Sourced from env (so it tracks the live deployment)
# with a few stable aliases. Extend freely. Values are read at runtime from the
# resolved env; nothing is hardcoded that could drift from the actual deployment.
def build_address_book():
    book = {}
    # role addresses from env (present in the app's environment)
    for name, envvar in [
        ("mm",       "MM_ADDRESS"),
        ("relay",    "RELAY_ADDRESS"),
        ("worker",   "MM_TREASURY_WORKER_ADDRESS"),
        ("fee",      "TREASURY_ADDRESS"),
        ("treasury", "TREASURY_ADDRESS"),      # alias
        ("cold",     "VSP_COLD_RESERVE_ADDRESS"),
        ("reserve",  "VSP_COLD_RESERVE_ADDRESS"),  # alias
        ("deployer", "META_ADDRESS"),
        ("meta",     "META_ADDRESS"),
        ("batch",    "BATCH_ADDRESS"),
    ]:
        v = os.getenv(envvar, "").strip()
        if v:
            book[name] = v
    return book

# --- KMS wallet selection -----------------------------------------------------
# The --from wallet must be a KMS wallet (we sign with its key). Map friendly
# names to the env prefix kms_account_from_env expects.
KMS_FROM = {
    "relay":  "RELAY",
    "mm":     "MM",
    "worker": "MM_TREASURY_WORKER",
}

ERC20_ABI = [
    {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
     "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"","type":"address"},{"name":"","type":"uint256"}],"name":"transfer",
     "outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],
     "stateMutability":"view","type":"function"},
]

def main():
    ap = argparse.ArgumentParser(description="Send/drain from a KMS wallet (dry-run by default).")
    ap.add_argument("--from", dest="frm", required=True, choices=sorted(KMS_FROM),
                    help="KMS wallet to send FROM")
    ap.add_argument("--asset", required=True,
                    help="'native' (AVAX) or 'USDC'/'VSP'/a token env-name or 0x-address")
    ap.add_argument("--to", required=True,
                    help="recipient: an address-book name (cold/fee/deployer…) or 0x-address")
    ap.add_argument("--amount", required=True,
                    help="human amount (e.g. 1.5) or 'ALL' to drain")
    ap.add_argument("--execute", action="store_true",
                    help="actually broadcast (default is dry-run)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the type-to-confirm prompt (for scripted drains)")
    ap.add_argument("--gas-limit", type=int, default=21000,
                    help="gas limit for native transfer (default 21000)")
    args = ap.parse_args()

    # --- imports that only exist inside the app container ---
    try:
        from web3 import Web3
    except Exception as e:
        die(f"web3 import failed (run inside app container): {e}")
    try:
        from signing.kms_account import kms_account_from_env
    except Exception as e:
        die(f"could not import kms_account_from_env from signing.kms_account: {e}\n"
            f"       (are you running inside the app container with /app on path?)")

    rpc = os.getenv("RPC_URL", "").split(",")[0].strip()
    chain_id = os.getenv("CHAIN_ID", "").strip()
    if not rpc:      die("RPC_URL not set in environment")
    if not chain_id: die("CHAIN_ID not set in environment")
    chain_id = int(chain_id)
    w3 = Web3(Web3.HTTPProvider(rpc))
    if not w3.is_connected():
        die(f"cannot connect to RPC {rpc[:40]}…")

    book = build_address_book()

    # --- construct the sender (KMS) ---
    prefix = KMS_FROM[args.frm]
    try:
        acct = kms_account_from_env(prefix)
        sender = Web3.to_checksum_address(acct.address)
    except Exception as e:
        die(f"kms_account_from_env('{prefix}') failed: {e}")

    # --- resolve recipient (address book or literal) ---
    to_raw = args.to.strip()
    if to_raw.lower() in book:
        to_addr = book[to_raw.lower()]
        to_source = f"address-book['{to_raw.lower()}']"
    elif to_raw.startswith("0x") and len(to_raw) == 42:
        to_addr = to_raw
        to_source = "literal address"
    else:
        die(f"recipient '{to_raw}' is neither a known name {sorted(book)} nor a 0x-address")
    try:
        to_addr = Web3.to_checksum_address(to_addr)
    except Exception:
        die(f"resolved recipient is not a valid address: {to_addr!r}")
    if to_addr == "0x" + "0"*40:
        die("refusing to send to the zero address")
    if to_addr.lower() == sender.lower():
        die("refusing to send to self (from == to)")

    # --- resolve asset ---
    asset = args.asset.strip()
    native = asset.lower() in ("native", "avax")
    token_addr = None
    if not native:
        # token: env-name (USDC->USDC_ADDRESS) or literal address
        if asset.startswith("0x") and len(asset) == 42:
            token_addr = Web3.to_checksum_address(asset)
        else:
            envname = asset.upper() + "_ADDRESS" if not asset.upper().endswith("_ADDRESS") else asset.upper()
            v = os.getenv(envname, "").strip()
            if not v:
                die(f"token '{asset}' not resolvable (looked up env {envname})")
            token_addr = Web3.to_checksum_address(v)

    # --- compute amount ---
    drain = args.amount.strip().upper() == "ALL"
    gas_price = w3.eth.gas_price

    if native:
        bal = w3.eth.get_balance(sender)
        if drain:
            gas_cost = gas_price * args.gas_limit
            # small safety margin so a gas_price tick between quote and send can't underfund
            gas_cost = int(gas_cost * 1.10)
            value = bal - gas_cost
            if value <= 0:
                die(f"balance {w3.from_wei(bal,'ether')} AVAX too low to cover gas "
                    f"({w3.from_wei(gas_cost,'ether')} AVAX) — nothing to drain")
        else:
            value = w3.to_wei(args.amount, "ether")
            if value > bal:
                die(f"amount {args.amount} AVAX exceeds balance {w3.from_wei(bal,'ether')}")
        human_amt = f"{w3.from_wei(value,'ether')} AVAX"
    else:
        token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        try:
            decimals = token.functions.decimals().call()
        except Exception:
            decimals = 18
        try:
            symbol = token.functions.symbol().call()
        except Exception:
            symbol = "TOKEN"
        tbal = token.functions.balanceOf(sender).call()
        if drain:
            value = tbal
            if value == 0:
                die(f"{symbol} balance is 0 — nothing to drain")
        else:
            value = int(round(float(args.amount) * (10 ** decimals)))
            if value > tbal:
                die(f"amount {args.amount} {symbol} exceeds balance "
                    f"{tbal/(10**decimals)} {symbol}")
        human_amt = f"{value/(10**decimals)} {symbol}"
        # native gas still needed to send an ERC-20
        nbal = w3.eth.get_balance(sender)
        est_gas_cost = gas_price * 90000
        if nbal < est_gas_cost:
            die(f"sender has {w3.from_wei(nbal,'ether')} AVAX — insufficient for "
                f"~{w3.from_wei(est_gas_cost,'ether')} AVAX gas to send the token")

    # --- summary (always printed) ---
    print("┌─ vsp-kms-send ─────────────────────────────────────────")
    print(f"│ FROM   : {args.frm:8s} {sender}  (KMS: {prefix})")
    print(f"│ TO     : {to_addr}  ({to_source})")
    print(f"│ ASSET  : {'native AVAX' if native else f'{symbol} @ {token_addr}'}")
    print(f"│ AMOUNT : {human_amt}" + ("   [DRAIN ALL]" if drain else ""))
    print(f"│ chain  : {chain_id}   gasPrice: {w3.from_wei(gas_price,'gwei')} gwei")
    print(f"│ MODE   : {'EXECUTE (will broadcast)' if args.execute else 'DRY-RUN (no broadcast)'}")
    print("└────────────────────────────────────────────────────────")

    if not args.execute:
        print("[kms-send] dry-run only. Re-run with --execute to broadcast.")
        return

    # --- confirmation gate ---
    if not args.yes:
        typed = input(f"Type the recipient address to confirm send: ").strip()
        if typed.lower() != to_addr.lower():
            die("confirmation mismatch — aborted (nothing sent)")

    # --- build, sign (KMS), broadcast ---
    nonce = w3.eth.get_transaction_count(sender, "pending")
    if native:
        tx = {"from": sender, "to": to_addr, "value": value, "nonce": nonce,
              "gas": args.gas_limit, "gasPrice": gas_price, "chainId": chain_id}
    else:
        tx = token.functions.transfer(to_addr, value).build_transaction(
            {"from": sender, "nonce": nonce, "gas": 90000,
             "gasPrice": gas_price, "chainId": chain_id})
    try:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        h = w3.eth.send_raw_transaction(raw)
    except Exception as e:
        die(f"sign/broadcast failed: {e}")
    print(f"[kms-send] broadcast: {h.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    status = "SUCCESS" if rcpt.status == 1 else "FAILED"
    print(f"[kms-send] receipt: block {rcpt.blockNumber}  status={status}")
    if rcpt.status != 1:
        sys.exit(1)

if __name__ == "__main__":
    main()
