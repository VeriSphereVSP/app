# app/app/chain.py
from __future__ import annotations
from typing import Optional, Dict


# TODO: replace with real protocol calls
# This is a correct interface, even if mocked initially


def resolve_claim_on_chain(normalized_text: str) -> Optional[Dict[str, int]]:
    """
    Returns claim identity if it exists on-chain.
    """
    # Phase 1: exact match only
    # Later: semantic match / hash match
    return None


def fetch_claim_state(claim_id: int) -> Dict:
    """
    Fetches effective VS and economic metadata from protocol.
    """
    # Placeholder — structure is final even if values are mocked
    return {
        "eVS": 0,
        "stake": {
            "support": 0,
            "challenge": 0,
            "total": 0,
        },
        "links": {
            "incoming": 0,
            "outgoing": 0,
        },
    }

