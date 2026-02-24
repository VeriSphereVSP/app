# app/chain/abi.py
"""
Loads contract ABIs directly from Foundry build artifacts in core/out/.
No manual ABI maintenance — run `forge build` and ABIs are always current.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

# Path from app/ to core/out/
# app/ is at ~/verisphere/app/, core/out/ is at ~/verisphere/core/out/
_CORE_OUT = Path(__file__).resolve().parents[2] / "core" / "out"


def load_abi(contract_name: str) -> List[Dict[str, Any]]:
    """
    Load ABI for a contract from Foundry's build output.

    Args:
        contract_name: e.g. "PostRegistry", "StakeEngine"

    Returns:
        ABI as a list of dicts.

    Raises:
        FileNotFoundError if the artifact doesn't exist.
        Run `forge build` in core/ to generate artifacts.
    """
    artifact = _CORE_OUT / f"{contract_name}.sol" / f"{contract_name}.json"

    if not artifact.exists():
        raise FileNotFoundError(
            f"ABI artifact not found: {artifact}\n"
            f"Run: cd ~/verisphere/core && forge build"
        )

    with artifact.open() as f:
        data = json.load(f)

    abi = data.get("abi")
    if not abi:
        raise ValueError(f"No 'abi' key in {artifact}")

    return abi


# Pre-load all ABIs used by the app.
# These fail loudly at startup if forge build hasn't been run.
try:
    POST_REGISTRY_ABI   = load_abi("PostRegistry")
    STAKE_ENGINE_ABI    = load_abi("StakeEngine")
    SCORE_ENGINE_ABI    = load_abi("ScoreEngine")
    LINK_GRAPH_ABI      = load_abi("LinkGraph")
    PROTOCOL_VIEWS_ABI  = load_abi("ProtocolViews")
    VSP_TOKEN_ABI       = load_abi("VSPToken")
except FileNotFoundError as e:
    logger.error(str(e))
    raise
