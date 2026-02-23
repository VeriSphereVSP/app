# app/main.py
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
import json

from sqlalchemy.orm import Session
from sqlalchemy import text

from db import get_db
from llm import interpret_with_openai
from merge import merge_article_with_chain
from mm_pricing import compute_mm_prices
from erc20 import allowance, transfer, transfer_from
from config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS
from semantic import compute_one
from chain.claim_registry import create_claim
from chain.stake import stake_claim
from relay import router as relay_router

app = FastAPI(title="VeriSphere App API", version="0.1.0")

# Mount the meta-transaction relay router
app.include_router(relay_router)

# Read from addresses.json (proxy addresses) instead of run-latest.json
ADDRESSES_PATH = Path("/app/broadcast/Deploy.s.sol/43113/addresses.json")

@app.on_event("startup")
async def check_deployment():
    if not ADDRESSES_PATH.exists():
        print(f"Warning: Deployment artifact not found at {ADDRESSES_PATH}")
    else:
        print(f"Deployment artifact found: {ADDRESSES_PATH}")

# ------------------------------------------------------------
# Health & Contracts
# ------------------------------------------------------------
@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}

@app.get("/api/contracts")
def get_contracts():
    """Return deployed contract addresses (proxies) with USDC added."""
    if not ADDRESSES_PATH.exists():
        raise HTTPException(500, f"Deployment artifact not found at {ADDRESSES_PATH}")
    
    try:
        with ADDRESSES_PATH.open() as f:
            contracts = json.load(f)
        
        # Add external token addresses
        contracts["USDC"] = USDC_ADDRESS
        contracts["VSPToken"] = VSP_ADDRESS
        
        # Convert all addresses to lowercase for consistency
        contracts = {k: v.lower() if isinstance(v, str) else v for k, v in contracts.items()}
        
        print(f"Returning {len(contracts)} contracts from /api/contracts")
        return contracts
        
    except Exception as e:
        import traceback
        print("ERROR in /api/contracts:", str(e))
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to load contracts: {str(e)}")

# ------------------------------------------------------------
# Claim Status (simple helper)
# ------------------------------------------------------------
@app.get("/api/claim-status/{claim_text}")
def claim_status(claim_text: str, db: Session = Depends(get_db)):
    on_chain = compute_one(db, claim_text)
    stake_support = 0  # TODO: Query StakeEngine
    stake_challenge = 0
    return {
        "on_chain": on_chain,
        "stake_support": stake_support,
        "stake_challenge": stake_challenge,
        "author": "Unknown",
    }

# ------------------------------------------------------------
# LLM Interpretation
# ------------------------------------------------------------
class InterpretRequest(BaseModel):
    input: str
    model: Optional[str] = None

@app.post("/api/interpret")
def interpret(req: InterpretRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not req.input or not isinstance(req.input, str):
        raise HTTPException(status_code=400, detail="Invalid input")
    try:
        r = interpret_with_openai(req.input, model=req.model or "gpt-4o-mini")

        if r["kind"] == "claims":
            for claim in r["claims"]:
                claim_text = claim["text"]
                on_chain = compute_one(db, claim_text, top_k=5)
                claim["on_chain"] = on_chain
                claim["stake_support"] = 0
                claim["stake_challenge"] = 0
                claim["author"] = "User"

        elif r["kind"] == "article":
            for section in r["sections"]:
                for claim in section["claims"]:
                    claim_text = claim["text"]
                    on_chain = compute_one(db, claim_text, top_k=5)
                    claim["on_chain"] = on_chain
                    claim["stake_support"] = 0
                    claim["stake_challenge"] = 0
                    claim["author"] = "AI Search"

        return r
    except Exception as e:
        import traceback
        print("Interpret error:", str(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Interpret failed: {str(e)}")

# ------------------------------------------------------------
# Market Maker - Quote & Trade
# ------------------------------------------------------------
@app.get("/api/mm/quote")
def mm_quote(db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text("SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE")
        ).fetchone()

        if not row:
            print("Warning: mm_state row missing - returning fallback")
            return {
                "market_price_usdc": 1.0,
                "buy_usdc": 1.0025,
                "sell_usdc": 0.9975,
                "ts": datetime.utcnow().isoformat() + "Z",
            }

        net_vsp, unit_au, spread_rate = row

        prices = compute_mm_prices(net_vsp=net_vsp, unit_au=unit_au, spread_rate=spread_rate)

        return {
            "market_price_usdc": float(prices["market_usd"]),
            "buy_usdc": float(prices["buy_usd"]),
            "sell_usdc": float(prices["sell_usd"]),
            "ts": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        print(f"MM quote error: {str(e)}")
        raise HTTPException(500, f"Failed to get quote: {str(e)}")

class MMTradeRequest(BaseModel):
    user_address: str
    vsp_amount: int
    expected_price_usdc: float

@app.post("/api/mm/buy")
def mm_buy(req: MMTradeRequest, db: Session = Depends(get_db)):
    usdc_needed = int(req.vsp_amount * req.expected_price_usdc * 1_000_000)
    if allowance(USDC_ADDRESS, req.user_address, MM_ADDRESS) < usdc_needed:
        raise HTTPException(400, "USDC allowance too low")
    with db.begin():
        net_vsp, unit_au, spread_rate = db.execute(
            text(
                "SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE FOR UPDATE"
            )
        ).one()
        prices = compute_mm_prices(net_vsp, unit_au, spread_rate)
        transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, usdc_needed)
        transfer(VSP_ADDRESS, req.user_address, req.vsp_amount * 10**18)
        db.execute(
            text("UPDATE mm_state SET net_vsp = :n WHERE id = TRUE"),
            {"n": net_vsp + req.vsp_amount},
        )
    return {"ok": True}

@app.post("/api/mm/sell")
def mm_sell(req: MMTradeRequest, db: Session = Depends(get_db)):
    usdc_out = int(req.vsp_amount * req.expected_price_usdc * 1_000_000)
    if allowance(VSP_ADDRESS, req.user_address, MM_ADDRESS) < req.vsp_amount * 10**18:
        raise HTTPException(400, "VSP allowance too low")
    with db.begin():
        net_vsp, unit_au, spread_rate = db.execute(
            text(
                "SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE FOR UPDATE"
            )
        ).one()
        transfer_from(VSP_ADDRESS, req.user_address, MM_ADDRESS, req.vsp_amount * 10**18)
        transfer(USDC_ADDRESS, req.user_address, usdc_out)
        db.execute(
            text("UPDATE mm_state SET net_vsp = :n WHERE id = TRUE"),
            {"n": net_vsp - req.vsp_amount},
        )
    return {"ok": True}

# ------------------------------------------------------------
# Claim Creation & Staking (backend pays fees)
# ------------------------------------------------------------
class CreateClaimRequest(BaseModel):
    text: str = Field(..., min_length=3)

@app.post("/api/claims/create")
def create_claim_endpoint(req: CreateClaimRequest):
    try:
        tx_hash = create_claim(req.text)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create claim: {str(e)}")

class StakeRequest(BaseModel):
    claim_id: int = Field(..., ge=0)
    side: str = Field(..., pattern="^(support|challenge)$")
    amount: int = Field(..., gt=0)

@app.post("/api/claims/stake")
def stake_endpoint(req: StakeRequest):
    try:
        tx_hash = stake_claim(req.claim_id, req.side, req.amount)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to stake: {str(e)}")

class WithdrawRequest(BaseModel):
    claim_id: int = Field(..., ge=0)
    side: str = Field(..., pattern="^(support|challenge)$")
    amount: int = Field(..., gt=0)
    lifo: bool = Field(default=True)

@app.post("/api/claims/unstake")
def unstake_endpoint(req: WithdrawRequest):
    """Withdraw (unstake) VSP from a claim."""
    try:
        from chain.stake import withdraw_stake
        tx_hash = withdraw_stake(req.claim_id, req.side, req.amount, req.lifo)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to unstake: {str(e)}")
    
class CreateLinkRequest(BaseModel):
    independent_post_id: int = Field(..., ge=0)
    dependent_post_id: int = Field(..., ge=0)
    is_challenge: bool

@app.post("/api/links/create")
def create_link_endpoint(req: CreateLinkRequest):
    """Create a link between two claims."""
    try:
        from chain.claim_registry import create_link
        tx_hash = create_link(req.independent_post_id, req.dependent_post_id, req.is_challenge)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create link: {str(e)}")
