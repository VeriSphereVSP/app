# app/config.py
from dotenv import load_dotenv
from pathlib import Path
import os, json

load_dotenv()

# ------------------------------------------------------------
# Deployment artifact (Foundry broadcast JSON)
# ------------------------------------------------------------
BROADCAST_PATH = Path("/app/broadcast/Deploy.s.sol/43113/run-latest.json")

def load_deployed():
    if not BROADCAST_PATH.exists():
        print(f"⚠️ deployment artifact missing: {BROADCAST_PATH}")
        return {}

    try:
        with BROADCAST_PATH.open() as f:
            data = json.load(f)

        out = {}
        for tx in data.get("transactions", []):
            name = tx.get("contractName")
            addr = tx.get("contractAddress")
            if name and addr:
                out[name] = addr
        return out
    except Exception as e:
        print("⚠️ failed to load deployment JSON:", e)
        return {}

DEPLOYED = load_deployed()

# ------------------------------------------------------------
# Chain / contracts
# ------------------------------------------------------------
CHAIN_ID = int(os.getenv("CHAIN_ID", "43113"))

USDC_ADDRESS = (
    os.getenv("USDC_ADDRESS")
    or DEPLOYED.get("USDC")
    or "0x5425890298Aed601595a70AB815c96711a31Bc65"
)

VSP_ADDRESS = (
    os.getenv("VSP_ADDRESS")
    or DEPLOYED.get("VSPToken")
    or "0x4901d977dFec9A758E2715deD5DC55B4aaF8B610"
)

POST_REGISTRY_ADDRESS = DEPLOYED.get("PostRegistry")

MM_ADDRESS = os.getenv("MM_ADDRESS", "")
MM_PRIVATE_KEY = os.getenv("MM_PRIVATE_KEY", "")

RPC_URL = os.getenv(
    "RPC_URL",
    "https://api.avax-test.network/ext/bc/C/rpc"
)

# ------------------------------------------------------------
# App / DB / LLM
# ------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "stub")
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-large")

DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.95"))
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.85"))

print("Config loaded:")
print("  CHAIN_ID:", CHAIN_ID)
print("  POST_REGISTRY:", POST_REGISTRY_ADDRESS)
print("  MM_ADDRESS:", MM_ADDRESS)
print("  RPC_URL:", RPC_URL[:48] + "…")

