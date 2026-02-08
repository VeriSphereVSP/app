from __future__ import annotations

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Any, Dict, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text
from pathlib import Path
import json

from .llm import interpret_with_openai
from .db import get_db
from app.mm_pricing import compute_mm_prices
from .erc20 import allowance, transfer, transfer_from
from .config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS
from .semantic import compute_one
from .merge import merge_article_with_chain  # ← NEW: central merge logic

app = FastAPI(title="VeriSphere App API", version="0.1.0")

# ============================================================
# Deployment artifact path (mounted from core repo)
# ============================================================
BROADCAST_PATH = Path("/app/broadcast/Deploy.s.sol/43113/run-latest.json")


@app.on_event("startup")
async def check_deployment():
    if not BROADCAST_PATH.exists():
        print(f"Warning: Deployment artifact not found at {BROADCAST_PATH}")
    else:
        print(f"Deployment artifact found: {BROADCAST_PATH}")


@app.get("/api/contracts")
def get_contracts():
    if not BROADCAST_PATH.exists():
        raise HTTPException(500, f"Deployment artifact not found at {BROADCAST_PATH}")
    try:
        with BROADCAST_PATH.open() as f:
            data = json.load(f)
        contracts: Dict[str, str] = {}
        for tx in data.get("transactions", []):
            name = tx.get("contractName")
            addr = tx.get("contractAddress")
            if name and addr:
                contracts[name] = addr

        # Ensure USDC + VSP addresses are always present
        contracts.setdefault("USDC", USDC_ADDRESS)
        contracts.setdefault("VSPToken", VSP_ADDRESS)

        print(f"Returning {len(contracts)} contracts from /api/contracts")
        return contracts
    except Exception as e:
        import traceback

        print("ERROR in /api/contracts:", str(e))
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to load contracts: {str(e)}")


@app.get("/api/claim-status/{claim_text}")
def claim_status(claim_text: str, db: Session = Depends(get_db)):
    """
    Simple helper endpoint to check on-chain / semantic status
    for a single claim text.
    """
    on_chain = compute_one(db, claim_text)
    stake_support = 0  # TODO: Query StakeEngine
    stake_challenge = 0
    return {
        "on_chain": on_chain,
        "stake_support": stake_support,
        "stake_challenge": stake_challenge,
        "author": "Unknown",
    }


# ============================================================
# Existing endpoints
# ============================================================


class InterpretRequest(BaseModel):
    input: str
    model: Optional[str] = None


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"ok": "true"}


@app.post("/api/interpret")
def interpret(req: InterpretRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Main interpretation endpoint.

    1. Calls LLM to classify + decompose user input.
    2. Normalizes / merges with on-chain / semantic data via merge_article_with_chain.
    3. Returns a frontend-safe structure:

       - kind: "non_actionable" | "article"
       - if "article":
           {
             "kind": "article",
             "title": string,
             "sections": [
               {
                 "id": string,
                 "text": string,
                 "claims": [
                   {
                     "text": string,
                     "on_chain": bool,
                     "eVS": number,
                     "stake": { support, challenge, total } | null,
                     "links": { incoming, outgoing } | null
                   }
                 ]
               }
             ]
           }
    """
    if not req.input or not isinstance(req.input, str):
        raise HTTPException(status_code=400, detail="Invalid input")

    try:
        # Step 1: LLM classification / decomposition
        llm_result = interpret_with_openai(
            req.input,
            model=req.model or "gpt-4o-mini",
        )

        # Step 2: Merge with on-chain / semantic info
        merged = merge_article_with_chain(llm_result, db)

        return merged

    except Exception as e:
        import traceback

        print("Interpret error:", str(e))
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Interpret failed: {str(e)}",
        )


# ============================================================
# Market Maker API – prefixed with /api/mm/
# ============================================================


def oracle_timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"


@app.get("/api/mm/quote")
def mm_quote(db: Session = Depends(get_db)):
    net_vsp, unit_au, spread_rate = db.execute(
        text(
            """
            SELECT net_vsp, unit_au, spread_rate
            FROM mm_state
            WHERE id = TRUE
        """
        )
    ).one()
    prices = compute_mm_prices(
        net_vsp=net_vsp,
        unit_au=unit_au,
        spread_rate=spread_rate,
    )
    print("Quote requested – returning prices")
    return {
        "market_price_usdc": prices["market_usd"],
        "buy_usdc": prices["buy_usd"],
        "sell_usdc": prices["sell_usd"],
        "ts": oracle_timestamp(),
    }


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
        prices = compute_mm_prices(net_vsp, unit_au, spread_rate)  # noqa: F841
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

