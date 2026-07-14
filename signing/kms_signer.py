#!/usr/bin/env python3
"""kms_signer.py — a web3-compatible signer whose private key lives in GCP KMS.

A pluggable BACKEND supplies two byte-strings: the SPKI public key (KMS
getPublicKey) and a DER ECDSA signature over a 32-byte digest (KMS
asymmetricSign). All Ethereum work is done here with STANDARD RLP encoding
(no eth_account internals — those move between versions), so the full pipeline
(tx dict -> raw signed tx -> recovered sender) is proven locally with
LocalKeyBackend and runs unchanged in prod with KMSBackend.

Drop-in for Account.from_key(...):
    signer = KMSSigner(KMSBackend(key_version_resource))
    signer.address
    raw = signer.sign_transaction(tx_dict, chain_id)   # hex raw tx to broadcast
"""
from __future__ import annotations

from typing import Protocol

import rlp
from eth_utils import keccak, to_bytes, to_hex

from signing import kms_eth


class SignerBackend(Protocol):
    def public_key_spki(self) -> bytes: ...
    def sign_digest_der(self, digest32: bytes) -> bytes: ...


# ── backends ────────────────────────────────────────────────────────────────
class LocalKeyBackend:
    """TEST ONLY — holds a raw key, mimics KMS. Never use in prod."""
    def __init__(self, privkey_hex: str):
        from ecdsa import SigningKey, SECP256k1
        self._sk = SigningKey.from_string(
            bytes.fromhex(privkey_hex.removeprefix("0x")), curve=SECP256k1)

    def public_key_spki(self) -> bytes:
        return self._sk.get_verifying_key().to_der()

    def sign_digest_der(self, digest32: bytes) -> bytes:
        from ecdsa.util import sigencode_der
        return self._sk.sign_digest(digest32, sigencode=sigencode_der)


class KMSBackend:
    """Prod — GCP Cloud KMS. The Ethereum keccak digest is passed into the
    'sha256' digest field; KMS signs a provided digest as-is (no re-hash)."""
    def __init__(self, key_version_resource: str, client=None):
        from google.cloud import kms
        self._kms = client or kms.KeyManagementServiceClient()
        self._name = key_version_resource

    def public_key_spki(self) -> bytes:
        pem = self._kms.get_public_key(request={"name": self._name}).pem
        from cryptography.hazmat.primitives.serialization import (
            load_pem_public_key, Encoding, PublicFormat)
        return load_pem_public_key(pem.encode()).public_bytes(
            Encoding.DER, PublicFormat.SubjectPublicKeyInfo)

    def sign_digest_der(self, digest32: bytes) -> bytes:
        return self._kms.asymmetric_sign(
            request={"name": self._name, "digest": {"sha256": digest32}}).signature


# ── rlp helpers ─────────────────────────────────────────────────────────────
def _b(x) -> bytes:
    if x in (None, "", b"", 0):
        return b""
    if isinstance(x, bytes):
        return x
    if isinstance(x, int):
        return x.to_bytes((x.bit_length() + 7) // 8, "big")
    if isinstance(x, str):
        return to_bytes(hexstr=x) if x.startswith("0x") else to_bytes(text=x)
    return to_bytes(x)


def _to(addr) -> bytes:
    return b"" if not addr else to_bytes(hexstr=addr)


# ── signer ──────────────────────────────────────────────────────────────────
class KMSSigner:
    def __init__(self, backend: SignerBackend):
        self._b = backend
        self.address = kms_eth.address_from_spki(backend.public_key_spki())

    def _sign(self, digest32: bytes):
        der = self._b.sign_digest_der(digest32)
        return kms_eth.assemble(digest32, der, self.address)  # (r, s, recid)

    def sign_transaction(self, tx: dict, chain_id: int) -> str:
        tx = dict(tx); tx.setdefault("chainId", chain_id)
        typed = ("maxFeePerGas" in tx or "maxPriorityFeePerGas" in tx
                 or tx.get("type") in (2, "0x2"))
        if typed:
            access = tx.get("accessList", []) or []
            body = [_b(tx["chainId"]), _b(tx["nonce"]), _b(tx["maxPriorityFeePerGas"]),
                    _b(tx["maxFeePerGas"]), _b(tx["gas"]), _to(tx.get("to")),
                    _b(tx.get("value", 0)), _b(tx.get("data", b"")), access]
            digest = keccak(b"\x02" + rlp.encode(body))
            r, s, recid = self._sign(digest)
            signed = body + [_b(recid), _b(r), _b(s)]
            return to_hex(b"\x02" + rlp.encode(signed))
        # legacy, EIP-155
        body = [_b(tx["nonce"]), _b(tx["gasPrice"]), _b(tx["gas"]), _to(tx.get("to")),
                _b(tx.get("value", 0)), _b(tx.get("data", b""))]
        digest = keccak(rlp.encode(body + [_b(tx["chainId"]), _b(0), _b(0)]))
        r, s, recid = self._sign(digest)
        v = kms_eth.legacy_v(recid, tx["chainId"])
        return to_hex(rlp.encode(body + [_b(v), _b(r), _b(s)]))


# ── full-pipeline self test: tx dict -> raw -> recovered sender ──────────────
def self_test() -> None:
    from eth_account import Account
    import os
    for _ in range(50):
        pk = "0x" + os.urandom(32).hex()
        ref = Account.from_key(pk)
        signer = KMSSigner(LocalKeyBackend(pk))
        assert signer.address.lower() == ref.address.lower(), "address mismatch"
        legacy = {"nonce": 7, "gasPrice": 25_000_000_000, "gas": 21000,
                  "to": "0x" + os.urandom(20).hex(), "value": 10**16}
        tx1559 = {"nonce": 3, "maxPriorityFeePerGas": 1_500_000_000,
                  "maxFeePerGas": 30_000_000_000, "gas": 120000,
                  "to": "0x" + os.urandom(20).hex(), "value": 0,
                  "data": b"\xab\xcd", "type": 2}
        for tx in (legacy, tx1559):
            raw = signer.sign_transaction(dict(tx), 43113)
            assert Account.recover_transaction(raw).lower() == ref.address.lower(), \
                f"sender mismatch ({tx.get('type','legacy')})"
    print("kms_signer.self_test: OK (50x legacy + EIP-1559: raw tx recovers to KMS address)")


if __name__ == "__main__":
    self_test()
