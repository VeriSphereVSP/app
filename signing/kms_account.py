#!/usr/bin/env python3
"""kms_account.py — an eth_account-compatible shim over KMSSigner.

tx_signer.make_sign_and_send() touches an account in exactly two ways:
    account.address
    account.sign_transaction(tx).raw_transaction
So a KMS-backed object exposing those two is a drop-in for Account.from_key(...)
across relay / MM / treasury wallets, with NO changes to tx_signer.py or callers.

Usage:
    from signing.kms_account import kms_account_from_env
    account = kms_account_from_env("RELAY")   # reads RELAY_KMS_KEY + RELAY_ADDRESS
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _Signed:
    """Mimics eth_account's SignedTransaction for the attribute callers use."""
    raw_transaction: bytes

    # legacy alias (older web3/eth_account used camelCase)
    @property
    def rawTransaction(self) -> bytes:  # noqa: N802
        return self.raw_transaction


class KMSAccount:
    """Drop-in for eth_account.Account, backed by a GCP KMS key."""

    def __init__(self, key_resource: str, chain_id: int):
        from signing.kms_signer import KMSBackend, KMSSigner
        self._signer = KMSSigner(KMSBackend(key_resource))
        self._chain_id = int(chain_id)
        self.address = self._signer.address
        self.key_resource = key_resource

    def sign_transaction(self, tx: dict) -> _Signed:
        raw_hex = self._signer.sign_transaction(dict(tx), self._chain_id)
        raw = raw_hex if isinstance(raw_hex, bytes) else bytes.fromhex(
            raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
        return _Signed(raw_transaction=raw)

    def __repr__(self) -> str:  # never leak the resource path in logs by default
        return f"<KMSAccount {self.address}>"


class _DeferredKMSAccount:
    """Local-dev stand-in that postpones all KMS access to first signing use.

    Only constructed when VSP_KMS_DEFER=1. The address is taken on trust from
    {prefix}_ADDRESS (no startup derivation check — that check IS the KMS
    call), so reads and encoding work without GCP credentials; anything that
    actually signs still fails loud at call time.
    """

    def __init__(self, key_resource: str, address: str, chain_id: int):
        self._key_resource = key_resource
        self._chain_id = chain_id
        self._real: KMSAccount | None = None
        self.address = address

    def sign_transaction(self, tx: dict) -> _Signed:
        if self._real is None:
            if not self._key_resource:
                raise RuntimeError(
                    "kms_account: signing requested but no KMS key was configured "
                    "(running with VSP_KMS_DEFER=1).")
            self._real = KMSAccount(self._key_resource, self._chain_id)
            if self._real.address.lower() != self.address.lower():
                raise RuntimeError(
                    f"kms_account: deferred KMS key derives {self._real.address}, "
                    f"expected {self.address}")
        return self._real.sign_transaction(tx)

    def __repr__(self) -> str:
        return f"<DeferredKMSAccount {self.address}>"


def kms_account_from_env(prefix: str) -> KMSAccount:
    """Build a KMSAccount from env. FAIL LOUD, mirroring relay_wallet's policy.

    Reads:
      {prefix}_KMS_KEY   full KMS cryptoKeyVersion resource path (required)
      {prefix}_ADDRESS   expected address (required; asserted against the key)
      CHAIN_ID           chain id for EIP-155 / typed-tx signing

    VSP_KMS_DEFER=1 (local dev ONLY) skips the import-time KMS round-trip and
    returns a deferred account whose address comes from {prefix}_ADDRESS; the
    key/address assertion then happens on first signing call instead of at
    startup. Never set this in production — the startup assertion exists to
    catch a mis-pasted key resource before it can sign anything.
    """
    res = os.getenv(f"{prefix}_KMS_KEY", "").strip()
    expected = os.getenv(f"{prefix}_ADDRESS", "").strip()
    chain_id = os.getenv("CHAIN_ID", "").strip()
    if os.getenv("VSP_KMS_DEFER", "").strip() == "1":
        if not expected or not chain_id:
            raise RuntimeError(
                f"kms_account: {prefix}_ADDRESS and CHAIN_ID are still required "
                "with VSP_KMS_DEFER=1.")
        logger.warning(
            "kms_account: %s signer DEFERRED (VSP_KMS_DEFER=1) — address taken "
            "from env unverified; signing fails loud on first use without KMS",
            prefix)
        return _DeferredKMSAccount(res, expected, int(chain_id))  # type: ignore[return-value]
    if not res:
        raise RuntimeError(
            f"kms_account: {prefix}_KMS_KEY not set. KMS signing is required and "
            "fail-loud: no fallback to a raw private key.")
    if not expected:
        raise RuntimeError(
            f"kms_account: {prefix}_ADDRESS not set. Required so startup asserts the "
            "KMS key derives the expected address (guards a mis-pasted resource path).")
    if not chain_id:
        raise RuntimeError("kms_account: CHAIN_ID not set.")

    acct = KMSAccount(res, int(chain_id))
    if acct.address.lower() != expected.lower():
        raise RuntimeError(
            f"kms_account: {prefix}_KMS_KEY derives {acct.address}, expected "
            f"{prefix}_ADDRESS {expected}")
    logger.info("kms_account: %s signer ready via KMS, address=%s", prefix, acct.address)
    return acct
