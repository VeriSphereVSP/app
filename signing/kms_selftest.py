#!/usr/bin/env python3
"""kms_selftest.py — Phase 2 gate. Proves the REAL GCP KMS path works, and derives
the Ethereum address for each key. Run BEFORE rewiring any wallet.

For each key it:
  1. fetches the public key (KMS getPublicKey) and derives the Ethereum address,
  2. signs a known keccak digest via KMS asymmetricSign (the digest goes in the
     'sha256' field -- KMS signs a provided digest as-is, it does not re-hash),
  3. asserts the assembled (r,s,v) recovers to the SAME address,
  4. signs a real EIP-1559 tx and a legacy tx, and asserts eth_account recovers
     the sender as that address (full pipeline, not just the digest).

Exit 0 only if every key passes every check. Nothing is written; no chain access.

Usage (inside the app container, where google-cloud-kms is installed):
    python -m signing.kms_selftest \
        --project verisphere --location us-central1 --keyring verisphere-signers

Keys checked (override with --keys): treasury-worker, market-maker, relay
"""
from __future__ import annotations

import argparse
import sys

from signing import kms_eth
from signing.kms_signer import KMSBackend, KMSSigner

DEFAULT_KEYS = ["treasury-worker", "market-maker", "relay"]
GRN = "\033[32m"; RED = "\033[31m"; YEL = "\033[33m"; RST = "\033[0m"


def resource(project: str, location: str, keyring: str, key: str, version: str = "1") -> str:
    return (f"projects/{project}/locations/{location}/keyRings/{keyring}"
            f"/cryptoKeys/{key}/cryptoKeyVersions/{version}")


def check_key(name: str, res: str, chain_id: int) -> tuple[bool, str | None]:
    from eth_account import Account
    from eth_utils import keccak

    print(f"\n── {name} ──")
    print(f"  resource: {res}")
    try:
        backend = KMSBackend(res)
    except Exception as e:
        print(f"  {RED}FAIL{RST} could not construct KMSBackend: {e}")
        return False, None

    # 1. public key -> address
    try:
        spki = backend.public_key_spki()
        addr = kms_eth.address_from_spki(spki)
        print(f"  {GRN}OK{RST}   getPublicKey -> address {addr}")
    except Exception as e:
        print(f"  {RED}FAIL{RST} getPublicKey/address derivation: {e}")
        return False, None

    # 2. sign a known digest, assemble, recover
    try:
        digest = keccak(b"verisphere-kms-selftest")
        der = backend.sign_digest_der(digest)
        r, s, recid = kms_eth.assemble(digest, der, addr)
        from eth_keys.datatypes import Signature
        rec = Signature(vrs=(recid, r, s)).recover_public_key_from_msg_hash(digest)
        if rec.to_checksum_address().lower() != addr.lower():
            print(f"  {RED}FAIL{RST} digest sig recovered {rec.to_checksum_address()}, expected {addr}")
            return False, addr
        print(f"  {GRN}OK{RST}   asymmetricSign(digest) -> (r,s,v={recid}) recovers to key address")
    except Exception as e:
        print(f"  {RED}FAIL{RST} digest sign/recover: {e}")
        return False, addr

    # 3. full tx pipeline: EIP-1559 + legacy
    try:
        signer = KMSSigner(backend)
        if signer.address.lower() != addr.lower():
            print(f"  {RED}FAIL{RST} KMSSigner.address mismatch")
            return False, addr
        txs = {
            "EIP-1559": {"nonce": 0, "maxPriorityFeePerGas": 1_500_000_000,
                         "maxFeePerGas": 30_000_000_000, "gas": 21000,
                         "to": "0x000000000000000000000000000000000000dEaD",
                         "value": 0, "data": b"", "type": 2},
            "legacy": {"nonce": 0, "gasPrice": 25_000_000_000, "gas": 21000,
                       "to": "0x000000000000000000000000000000000000dEaD",
                       "value": 0, "data": b""},
        }
        for label, tx in txs.items():
            raw = signer.sign_transaction(dict(tx), chain_id)
            got = Account.recover_transaction(raw)
            if got.lower() != addr.lower():
                print(f"  {RED}FAIL{RST} {label}: raw tx recovers to {got}, expected {addr}")
                return False, addr
            print(f"  {GRN}OK{RST}   {label} tx signed -> sender recovers to key address")
    except Exception as e:
        print(f"  {RED}FAIL{RST} tx pipeline: {e}")
        return False, addr

    return True, addr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--location", required=True)
    ap.add_argument("--keyring", required=True)
    ap.add_argument("--version", default="1")
    ap.add_argument("--chain-id", type=int, default=43113)  # Fuji
    ap.add_argument("--keys", nargs="*", default=DEFAULT_KEYS)
    a = ap.parse_args()

    print("kms_selftest — proving the REAL KMS signing path (no chain access, no writes)")
    print(f"project={a.project} location={a.location} keyring={a.keyring} chain_id={a.chain_id}")

    results: dict[str, tuple[bool, str | None]] = {}
    for k in a.keys:
        results[k] = check_key(k, resource(a.project, a.location, a.keyring, k, a.version), a.chain_id)

    print("\n──────────── DERIVED ADDRESSES ────────────")
    for k, (ok, addr) in results.items():
        mark = f"{GRN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
        print(f"  {mark}  {k:<16} {addr or '<unresolved>'}")
    print("───────────────────────────────────────────")

    failed = [k for k, (ok, _) in results.items() if not ok]
    if failed:
        print(f"{RED}GATE FAILED{RST} — do NOT rewire any wallet. Failing keys: {', '.join(failed)}")
        return 8
    print(f"{GRN}GATE PASSED{RST} — KMS signing verified for all keys.")
    print(f"{YEL}Next:{RST} record these addresses, then fund/re-grant in order: relay -> MM -> worker.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
