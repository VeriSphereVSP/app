# app/app/config.py
from dotenv import load_dotenv
import os
load_dotenv()
from pathlib import Path
import json

# ============================================================
# Deployment artifact path (mounted from core repo)
# ============================================================
BROADCAST_PATH = Path("/app/broadcast/Deploy.s.sol/43113/run-latest.json")

def load_deployed_addresses():
    """Read addresses from latest Foundry deployment JSON (fallback if missing)."""
    if not BROADCAST_PATH.exists():
        print(f"Warning: Deployment artifact not found at {BROADCAST_PATH}")
        return {}

    try:
        with BROADCAST_PATH.open() as f:
            data = json.load(f)
        contracts = {}
        for tx in data.get("transactions", []):
            name = tx.get("contractName")
            addr = tx.get("contractAddress")
            if name and addr:
                contracts[name] = addr
        return contracts
    except Exception as e:
        print(f"Error loading deployment JSON: {e}")
        return {}

DEPLOYED = load_deployed_addresses()

# ============================================================
# Core / Chain
# ============================================================
CHAIN_ID = int(os.getenv("CHAIN_ID", "43113"))  # Fuji default

# ============================================================
# Contract addresses – prefer deployed JSON, fallback to env/hardcode
# ============================================================

USDC_ADDRESS = (
    os.getenv("USDC_ADDRESS")
    or DEPLOYED.get("USDC")
    or "0x5425890298Aed601595a70AB815c96711a31Bc65"  # Official Fuji testnet USDC
)

VSP_ADDRESS = (
    os.getenv("VSP_ADDRESS")
    or DEPLOYED.get("VSPToken")
    or "0x4901d977dFec9A758E2715deD5DC55B4aaF8B610"  # Your latest Fuji VSP
)

MM_ADDRESS = os.getenv("MM_ADDRESS", "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a")

# ============================================================
# Market Maker / Wallet
# ============================================================
MM_PRIVATE_KEY = os.getenv("MM_PRIVATE_KEY", "")  # Required for signing txs

RPC_URL = (
    os.getenv("RPC_URL")
    or "https://api.avax-test.network/ext/bc/C/rpc"  # Public Fuji fallback
)

# ============================================================
# LLM / Embeddings
# ============================================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

DATABASE_URL = os.getenv("DATABASE_URL", "")

EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "stub").lower()
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-large")

DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.95"))
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.85"))

# ============================================================
# Debug print on import (remove/comment in prod)
# ============================================================
print("Config loaded:")
print(f"  CHAIN_ID: {CHAIN_ID}")
print(f"  USDC_ADDRESS: {USDC_ADDRESS}")
print(f"  VSP_ADDRESS: {VSP_ADDRESS}")
print(f"  MM_ADDRESS: {MM_ADDRESS}")
print(f"  RPC_URL: {RPC_URL[:50]}{'...' if len(RPC_URL) > 50 else ''}")
print(f"  MM_PRIVATE_KEY: {'<set>' if MM_PRIVATE_KEY else '<missing>'}")
print(f"  From deployment JSON: {len(DEPLOYED)} contracts found")
