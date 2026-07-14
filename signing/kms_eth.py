#!/usr/bin/env python3
"""kms_eth.py — turn a GCP Cloud KMS secp256k1 ECDSA signature into an Ethereum
signature (r, s, v), and derive the Ethereum address from a KMS public key.

This module is the risky, must-be-correct core of the KMS signer. It is written so
the hard parts (DER parsing, low-s normalization, recovery-id search, address
derivation) can be proven against a LOCAL key with no GCP dependency — see
self_test(). The KMS adapter (kms_client.py) only supplies two byte strings:
  - the SPKI/DER public key bytes  (from KMS getPublicKey)
  - the DER ECDSA signature bytes  (from KMS asymmetricSign over a 32-byte digest)
Everything Ethereum-specific happens here.

GCP gotchas this handles / flags (verify on first real-KMS run — self_test on the
box gates it): (1) KMS returns DER ECDSA (r,s) with NO recovery id — we recover v
by trying both and matching the known address. (2) Ethereum requires low-s
(EIP-2); KMS may return high-s — we normalize. (3) For Ethereum we must sign a
keccak256 digest, so the caller passes keccak(tx) into KMS's digest field; KMS
signs those 32 bytes as-is (it does not re-hash). (4) Legacy EIP-155 v = recid+35+
2*chainId; typed-tx (EIP-1559) v = recid (0/1). We return recid and both helpers.
"""
from __future__ import annotations

from typing import Tuple

from eth_keys import keys
from eth_keys.datatypes import Signature
from eth_utils import keccak

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_N = SECP256K1_N // 2


# ── DER parsing ─────────────────────────────────────────────────────────────
def der_to_rs(der: bytes) -> Tuple[int, int]:
    """Minimal DER SEQUENCE{ INTEGER r, INTEGER s } parser (no ASN.1 lib)."""
    if not der or der[0] != 0x30:
        raise ValueError("not a DER SEQUENCE")
    idx = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)

    def _int(i: int) -> Tuple[int, int]:
        if der[i] != 0x02:
            raise ValueError("expected DER INTEGER")
        ln = der[i + 1]
        start = i + 2
        val = int.from_bytes(der[start:start + ln], "big")
        return val, start + ln

    r, i = _int(idx)
    s, _ = _int(i)
    return r, s


def normalize_low_s(s: int) -> int:
    """EIP-2: s must be <= n/2, else use n - s."""
    return SECP256K1_N - s if s > _HALF_N else s


# ── public key / address ────────────────────────────────────────────────────
def _uncompressed_from_spki(spki_der: bytes) -> bytes:
    """Extract the 65-byte uncompressed point (0x04 || X || Y) from an EC SPKI DER.
    The uncompressed point is the trailing BIT STRING payload; for secp256k1 it is
    the last 65 bytes beginning with 0x04."""
    i = spki_der.rfind(b"\x04", 0, len(spki_der) - 64)
    # Fallback: the point is the final 65 bytes starting with 0x04.
    if spki_der[-65] == 0x04:
        return spki_der[-65:]
    if i != -1 and spki_der[i:i + 1] == b"\x04" and len(spki_der) - i >= 65:
        return spki_der[i:i + 65]
    raise ValueError("could not locate uncompressed EC point in SPKI")


def address_from_spki(spki_der: bytes) -> str:
    pub = _uncompressed_from_spki(spki_der)[1:]  # drop 0x04 -> 64 bytes X||Y
    return keys.PublicKey(pub).to_checksum_address()


def address_from_pubkey_xy(pub_xy: bytes) -> str:
    return keys.PublicKey(pub_xy).to_checksum_address()


# ── recovery id ─────────────────────────────────────────────────────────────
def recover_id(digest32: bytes, r: int, s: int, expected_addr: str) -> int:
    """Find recid in {0,1} whose recovered address matches expected_addr."""
    exp = expected_addr.lower()
    for recid in (0, 1):
        sig = Signature(vrs=(recid, r, s))
        try:
            pk = sig.recover_public_key_from_msg_hash(digest32)
        except Exception:
            continue
        if pk.to_checksum_address().lower() == exp:
            return recid
    raise ValueError("no recovery id matches expected address (bad sig/pubkey?)")


def assemble(digest32: bytes, der_sig: bytes, expected_addr: str) -> Tuple[int, int, int]:
    """DER sig + digest + known signer address -> (r, s, recid). recid is 0/1
    (EIP-1559 y_parity). Legacy v via legacy_v()."""
    r, s = der_to_rs(der_sig)
    s = normalize_low_s(s)
    recid = recover_id(digest32, r, s, expected_addr)
    return r, s, recid


def legacy_v(recid: int, chain_id: int) -> int:
    return recid + 35 + 2 * chain_id


# ── self test (no GCP): proves DER->rs, low-s, recovery, address derivation ──
def self_test() -> None:
    import os
    from ecdsa import SigningKey, SECP256k1
    from ecdsa.util import sigencode_der

    for _ in range(200):
        sk = SigningKey.generate(curve=SECP256k1)
        vk = sk.get_verifying_key()
        # local address (ground truth)
        pub_xy = vk.to_string()  # 64 bytes X||Y
        addr = address_from_pubkey_xy(pub_xy)

        # SPKI DER, as KMS getPublicKey would return, and check our extractor
        spki = vk.to_der()
        assert address_from_spki(spki).lower() == addr.lower(), "SPKI address mismatch"

        digest = keccak(os.urandom(64))  # stand-in for keccak(tx)
        der = sk.sign_digest(digest, sigencode=sigencode_der)  # DER (r,s), no recid

        r, s, recid = assemble(digest, der, addr)
        assert s <= _HALF_N, "s not normalized low"
        # verify the assembled (r,s,recid) recovers the signer
        rec = Signature(vrs=(recid, r, s)).recover_public_key_from_msg_hash(digest)
        assert rec.to_checksum_address().lower() == addr.lower(), "recover mismatch"
        # legacy v sanity (Fuji chainId 43113)
        v = legacy_v(recid, 43113)
        assert v in (86261, 86262), f"unexpected legacy v {v}"

    print("kms_eth.self_test: OK (200 random keys — DER, low-s, recid, address, v)")


if __name__ == "__main__":
    self_test()
