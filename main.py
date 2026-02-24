# app/main.py
import asyncio
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def lifespan(app):
    from chain.indexer import run_indexer
    indexer_task = asyncio.create_task(run_indexer())
    print("Blockchain event indexer started")
    yield
    indexer_task.cancel()
    try:
        await indexer_task
    except asyncio.CancelledError:
        pass
    print("Blockchain event indexer stopped")


app = FastAPI(title="VeriSphere App API", version="0.1.0", lifespan=lifespan)
app.include_router(relay_router)

ADDRESSES_PATH = Path("/app/broadcast/Deploy.s.sol/43113/addresses.json")


@app.get("/healthz")
def healthz():
    return {"ok": "true"}


@app.get("/api/contracts")
def get_contracts():
    if not ADDRESSES_PATH.exists():
        raise HTTPException(500, f"Deployment artifact not found at {ADDRESSES_PATH}")
    try:
        with ADDRESSES_PATH.open() as f:
            contracts = json.load(f)
        contracts["USDC"] = USDC_ADDRESS
        contracts["VSPToken"] = VSP_ADDRESS
        contracts = {k: v.lower() if isinstance(v, str) else v for k, v in contracts.items()}
        print(f"Returning {len(contracts)} contracts from /api/contracts")
        return contracts
    except Exception as e:
        import traceback
        print("ERROR in /api/contracts:", str(e))
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to load contracts: {str(e)}")


def _find_on_chain_match(db, on_chain_result, claim_text=None):
    """If this claim isn't on-chain but a similar on-chain claim exists, return that post_id."""
    if on_chain_result.get("post_id") is not None:
        return on_chain_result["post_id"]

    # First: check the similar results from compute_one
    for sim in on_chain_result.get("similar", []):
        sim_cid = sim.get("claim_id")
        if sim_cid is None:
            continue
        row = db.execute(
            text("SELECT post_id FROM claim WHERE claim_id = :id AND post_id IS NOT NULL"),
            {"id": sim_cid},
        ).fetchone()
        if row and sim.get("similarity", 0) >= 0.75:
            return int(row[0])

    # Second: directly query for on-chain claims similar to this text
    # This catches cases where the match isn't in the top-K general results
    if claim_text:
        try:
            from embedding import embed
            from config import EMBEDDINGS_MODEL
            vec = embed(claim_text)
            rows = db.execute(
                text("""
                    SELECT c.post_id,
                           1 - (ce.embedding <=> CAST(:vec AS vector)) AS similarity
                    FROM claim c
                    JOIN claim_embedding ce ON ce.claim_id = c.claim_id
                    WHERE c.post_id IS NOT NULL
                    ORDER BY ce.embedding <=> CAST(:vec AS vector)
                    LIMIT 1
                """),
                {"vec": vec},
            ).fetchone()
            if rows and rows[1] >= 0.75:
                return int(rows[0])
        except Exception as e:
            import traceback
            print(f"On-chain similarity search failed: {e}")
            print(traceback.format_exc())

    return None


@app.get("/api/claim-status/{claim_text}")
def claim_status(claim_text: str, user: str = None, db: Session = Depends(get_db)):
    """Return full claim state including on-chain stakes and verity score."""
    on_chain = compute_one(db, claim_text, top_k=10)

    # Check if this claim or a near-duplicate is on-chain
    post_id = _find_on_chain_match(db, on_chain, claim_text)

    # If we found a match via similarity, update on_chain to reflect it
    if post_id is not None and on_chain.get("post_id") is None:
        on_chain["post_id"] = post_id
        on_chain["matched_via"] = "similarity"

    result = {
        "on_chain": on_chain,
        "stake_support": 0,
        "stake_challenge": 0,
        "user_support": 0,
        "user_challenge": 0,
        "verity_score": 0.0,
        "author": "Unknown",
    }

    if post_id is not None:
        try:
            from chain.chain_reader import get_stake_totals, get_user_stake, get_verity_score
            support, challenge = get_stake_totals(post_id)
            result["stake_support"] = support
            result["stake_challenge"] = challenge
            result["verity_score"] = get_verity_score(post_id)

            if user:
                result["user_support"] = get_user_stake(user, post_id, 0)
                result["user_challenge"] = get_user_stake(user, post_id, 1)
        except Exception as e:
            import traceback
            print(f"Failed to read on-chain state for post_id={post_id}: {e}")
            print(traceback.format_exc())

    return result


class InterpretRequest(BaseModel):
    input: str
    model: Optional[str] = None


@app.post("/api/interpret")
def interpret(req: InterpretRequest, db: Session = Depends(get_db)):
    if not req.input or not isinstance(req.input, str):
        raise HTTPException(status_code=400, detail="Invalid input")
    try:
        r = interpret_with_openai(req.input, model=req.model or "gpt-4o-mini")

        if r["kind"] == "claims":
            for claim in r["claims"]:
                claim_text = claim["text"]
                on_chain = compute_one(db, claim_text, top_k=10)
                post_id = _find_on_chain_match(db, on_chain, claim_text)
                if post_id is not None and on_chain.get("post_id") is None:
                    on_chain["post_id"] = post_id
                    on_chain["matched_via"] = "similarity"
                claim["on_chain"] = on_chain
                claim["stake_support"] = 0
                claim["stake_challenge"] = 0
                claim["author"] = "User"

        elif r["kind"] == "article":
            for section in r["sections"]:
                for claim in section["claims"]:
                    claim_text = claim["text"]
                    on_chain = compute_one(db, claim_text, top_k=10)
                    post_id = _find_on_chain_match(db, on_chain, claim_text)
                    if post_id is not None and on_chain.get("post_id") is None:
                        on_chain["post_id"] = post_id
                        on_chain["matched_via"] = "similarity"
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


@app.get("/api/mm/quote")
def mm_quote(db: Session = Depends(get_db)):
    try:
        row = db.execute(
            text("SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE")
        ).fetchone()

        if not row:
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
    try:
        with db.begin():
            net_vsp, unit_au, spread_rate = db.execute(
                text("SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE FOR UPDATE")
            ).one()
            prices = compute_mm_prices(net_vsp, unit_au, spread_rate)
            transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, usdc_needed)
            transfer(VSP_ADDRESS, req.user_address, req.vsp_amount * 10**18)
            db.execute(
                text("UPDATE mm_state SET net_vsp = :n WHERE id = TRUE"),
                {"n": net_vsp + req.vsp_amount},
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"MM buy error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to buy VSP: {str(e)}")


@app.post("/api/mm/sell")
def mm_sell(req: MMTradeRequest, db: Session = Depends(get_db)):
    usdc_out = int(req.vsp_amount * req.expected_price_usdc * 1_000_000)
    if allowance(VSP_ADDRESS, req.user_address, MM_ADDRESS) < req.vsp_amount * 10**18:
        raise HTTPException(400, "VSP allowance too low")
    try:
        with db.begin():
            net_vsp, unit_au, spread_rate = db.execute(
                text("SELECT net_vsp, unit_au, spread_rate FROM mm_state WHERE id = TRUE FOR UPDATE")
            ).one()
            transfer_from(VSP_ADDRESS, req.user_address, MM_ADDRESS, req.vsp_amount * 10**18)
            transfer(USDC_ADDRESS, req.user_address, usdc_out)
            db.execute(
                text("UPDATE mm_state SET net_vsp = :n WHERE id = TRUE"),
                {"n": net_vsp - req.vsp_amount},
            )
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"MM sell error: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to sell VSP: {str(e)}")


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
    try:
        from chain.claim_registry import create_link
        tx_hash = create_link(req.independent_post_id, req.dependent_post_id, req.is_challenge)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create link: {str(e)}")