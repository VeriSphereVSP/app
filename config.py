# app/config.py
import os
from pathlib import Path
import json

# Network configuration
CHAIN_ID = int(os.getenv("CHAIN_ID", "43113"))
RPC_URL_READ = os.getenv("RPC_URL_READ", os.getenv("RPC_URL", "https://api.avax-test.network/ext/bc/C/rpc"))
RPC_URL = os.getenv("RPC_URL", "")

# Determine network name
if CHAIN_ID == 43113:
    NETWORK = "fuji"
elif CHAIN_ID == 43114:
    NETWORK = "mainnet"
else:
    NETWORK = f"chain-{CHAIN_ID}"

# Load deployed contract addresses
DEPLOYMENTS_DIR = Path(__file__).parent / "deployments"
ADDRESSES_FILE = DEPLOYMENTS_DIR / f"{NETWORK}.json"

if ADDRESSES_FILE.exists():
    with open(ADDRESSES_FILE) as f:
        DEPLOYED = json.load(f)
else:
    print(f"Warning: No deployment file at {ADDRESSES_FILE}")
    DEPLOYED = {}

# Contract addresses
AUTHORITY_ADDRESS = DEPLOYED.get("Authority", "")
VSP_TOKEN_ADDRESS = DEPLOYED.get("VSPToken", "")
POST_REGISTRY_ADDRESS = DEPLOYED.get("PostRegistry", "")
LINK_GRAPH_ADDRESS = DEPLOYED.get("LinkGraph", "")
STAKE_ENGINE_ADDRESS = DEPLOYED.get("StakeEngine", "")
SCORE_ENGINE_ADDRESS = DEPLOYED.get("ScoreEngine", "")
PROTOCOL_VIEWS_ADDRESS = DEPLOYED.get("ProtocolViews", "")
# Forwarder is deployed separately from core (see app/contracts/)
# Its address is either in the core deployment JSON (legacy) or in app/deployments/forwarder.json
FORWARDER_ADDRESS = DEPLOYED.get("Forwarder", "")
if not FORWARDER_ADDRESS:
    import json as _json
    _fwd_path = Path(__file__).parent / "deployments" / "forwarder.json"
    if _fwd_path.exists():
        FORWARDER_ADDRESS = _json.loads(_fwd_path.read_text()).get("Forwarder", "")


# External tokens
USDC_ADDRESS = os.getenv("USDC_ADDRESS", "0x5425890298aed601595a70ab815c96711a31bc65")
VSP_ADDRESS = VSP_TOKEN_ADDRESS

# Market maker wallet (reserves — backs outstanding VSP)
MM_ADDRESS = os.getenv("MM_ADDRESS", "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a")
MM_PRIVATE_KEY = os.getenv("MM_PRIVATE_KEY", "")

# Treasury wallet (revenue — receives trade fees + relay fees)
TREASURY_ADDRESS = os.getenv("TREASURY_ADDRESS", MM_ADDRESS)  # fallback to MM if not set

# Database
DB_USER = os.getenv("DB_USER", "verisphere")
DB_PASS = os.getenv("DB_PASS", "")
DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "verisphere")
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Embeddings
EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER", "openai")
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")

# Semantic search thresholds
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.95"))
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD", "0.85"))

# Print config (APP-08: password redacted)
_db_url_safe = f"postgresql://{DB_USER}:***@{DB_HOST}:{DB_PORT}/{DB_NAME}"
print("Config loaded:")
print(f"  CHAIN_ID: {CHAIN_ID}")
print(f"  NETWORK: {NETWORK}")
print(f"  POST_REGISTRY: {POST_REGISTRY_ADDRESS}")
print(f"  FORWARDER: {FORWARDER_ADDRESS}")
print(f"  MM_ADDRESS: {MM_ADDRESS}")
print(f"  RPC_URL: {RPC_URL[:50]}...")
print(f"  DB: {_db_url_safe}")


# Relay fee configuration
RELAY_FEE_MARGIN_PCT = float(os.getenv("RELAY_FEE_MARGIN_PCT", "0.30"))  # 30% margin on gas cost
RELAY_FEE_MIN_VSP = float(os.getenv("RELAY_FEE_MIN_VSP", "0.1"))  # Minimum relay fee
RELAY_FEE_TXN_PCT = float(os.getenv("RELAY_FEE_TXN_PCT", "0.01"))  # 1% of txn value
AVAX_PRICE_USD = float(os.getenv("AVAX_PRICE_USD", "20.0"))  # Fallback AVAX price
