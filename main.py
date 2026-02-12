# app/main.py
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Dict, Any, Optional
from pathlib import Path
import json

from sqlalchemy.orm import Session
from sqlalchemy import text

from db import get_db
from llm import interpret_with_openai
from merge import merge_article_with_chain
from chain.claim_registry import create_claim
from mm_pricing import compute_mm_prices
from config import USDC_ADDRESS, VSP_ADDRESS
from pydantic import BaseModel, Field

app = FastAPI(title="VeriSphere App API", version="0.1.0")

BROADCAST_PATH = Path("/app/broadcast/Deploy.s.sol/43113/run-latest.json")

# ------------------------------------------------------------
# Health
# ------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True}

# ------------------------------------------------------------
# Contracts (frontend discovery only)
# ------------------------------------------------------------
@app.get("/api/contracts")
def get_contracts():
    if not BROADCAST_PATH.exists():
        raise HTTPException(500, "deployment artifact missing")

    with BROADCAST_PATH.open() as f:
        data = json.load(f)

    out: Dict[str, str] = {}
    for tx in data.get("transactions", []):
        if tx.get("contractName") and tx.get("contractAddress"):
            out[tx["contractName"]] = tx["contractAddress"]

    out.setdefault("USDC", USDC_ADDRESS)
    out.setdefault("VSPToken", VSP_ADDRESS)
    return out

# ------------------------------------------------------------
# Claims
# ------------------------------------------------------------
class CreateClaimRequest(BaseModel):
    text: str

class CreateClaimResponse(BaseModel):
    txHash: str
    postId: Optional[int]

@app.post("/api/claims/create", response_model=CreateClaimResponse)
def api_create_claim(req: CreateClaimRequest):
    text = (req.text or "").strip()
    if len(text) < 3:
        raise HTTPException(400, "claim text too short")

    tx_hash, post_id = create_claim(text)
    return {"txHash": tx_hash, "postId": post_id}

# ------------------------------------------------------------
# LLM interpret
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

        # Enhance claims with on-chain data
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
        raise HTTPException(status_code=500, detail=f"Interpret failed: {str(e)}")

# ------------------------------------------------------------
# Market maker
# ------------------------------------------------------------

class MMTradeRequest(BaseModel):
    user_address: str
    vsp_amount: int
    expected_price_usdc: float


class BuyRequest(BaseModel):
    amount_usdc: int = Field(..., gt=0)
    max_price_usdc: float
    nonce: str

class SellRequest(BaseModel):
    amount_vsp: int = Field(..., gt=0)
    min_price_usdc: float
    nonce: str

@app.get("/api/mm/quote")
def mm_quote(db: Session = Depends(get_db)):
    net_vsp, unit_au, spread = db.execute(
        text("SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE")
    ).one()

    prices = compute_mm_prices(net_vsp, unit_au, spread)
    return prices

@app.post("/api/mm/buy")
def mm_buy(req: BuyRequest, db: Session = Depends(get_db)):
    return mm_execute("buy", req, db)

@app.post("/api/mm/sell")
def mm_sell(req: SellRequest, db: Session = Depends(get_db)):
    return mm_execute("sell", req, db)
