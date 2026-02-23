# app/config.py
import os
from pathlib import Path
import json

# Network configuration
CHAIN_ID = int(os.getenv("CHAIN_ID", "43113"))
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
FORWARDER_ADDRESS = DEPLOYED.get("Forwarder", "")

# External tokens
USDC_ADDRESS = os.getenv("USDC_ADDRESS", "0x5425890298aed601595a70ab815c96711a31bc65")
VSP_ADDRESS = VSP_TOKEN_ADDRESS

# Market maker wallet
MM_ADDRESS = os.getenv("MM_ADDRESS", "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a")
MM_PRIVATE_KEY = os.getenv("MM_PRIVATE_KEY", "")

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

# Print config
print("Config loaded:")
print(f"  CHAIN_ID: {CHAIN_ID}")
print(f"  NETWORK: {NETWORK}")
print(f"  POST_REGISTRY: {POST_REGISTRY_ADDRESS}")
print(f"  FORWARDER: {FORWARDER_ADDRESS}")
print(f"  MM_ADDRESS: {MM_ADDRESS}")
print(f"  RPC_URL: {RPC_URL[:50]}...")
