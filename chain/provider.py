# app/chain/provider.py — patch_trackb_shared_w3
# Track B prerequisite: the app-wide chain handle must not live in mm_wallet.
# mm_wallet imports KMS signing at module import (kms_account_from_env("MM"),
# fail-loud), so `from mm_wallet import w3` couples non-MM code paths to the MM
# key's existence and detonates on MM decommission. This module owns the shared
# handle; mm_wallet re-exports it for its signer until the MM is retired.
from config import RPC_WRITE_URLS
from tx_signer import build_w3

w3 = build_w3(RPC_WRITE_URLS)

__all__ = ["w3"]
