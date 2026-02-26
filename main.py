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
from config import USDC_ADDRESS, VSP_ADDRESS
from semantic import compute_one
from chain.claim_registry import create_claim
from chain.stake import stake_claim
from relay import router as relay_router
from mm_routes import router as mm_router
from claim_views import router as claim_views_router


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
app.include_router(mm_router)
app.include_router(claim_views_router)

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



@app.get("/api/claim-status/{claim_text}")
def claim_status(claim_text: str, user: str = None, db: Session = Depends(get_db)):
    """Return full claim state including on-chain stakes and verity score.
    Uses strict hash matching only — no fuzzy/similarity resolution."""
    on_chain = compute_one(db, claim_text, top_k=5)
    post_id = on_chain.get("post_id")

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
        r = interpret_with_openai(req.input)

        if r["kind"] == "non_actionable":
            return r

        # For single/multiple claims: generate an article about the implied topic
        # and include the user's claims in the set
        user_claims = []
        if r["kind"] == "claims":
            user_claims = r.get("claims", [])
            for uc in user_claims:
                uc["author"] = "User"

            # Extract the topic from the user's claims and generate an article
            topic_hint = "; ".join(c["text"] for c in user_claims)
            try:
                article_prompt = f"Write a factual overview about the topic implied by: {topic_hint}"
                article_r = interpret_with_openai(article_prompt, model=req.model or "gpt-4o-mini")
                if article_r["kind"] == "article":
                    r = article_r  # Replace with the article response
                elif article_r["kind"] == "claims":
                    # LLM returned claims again — use them as article claims
                    r["kind"] = "article"
                    r["title"] = r.get("title", "Results")
                    r["sections"] = [{"text": "", "claims": article_r.get("claims", [])}]
            except Exception as e:
                print(f"Topic article generation failed (non-fatal): {e}")
                # Fall through with just the user claims

        # Collect all AI claims
        ai_claims = []
        if r["kind"] == "claims":
            ai_claims = r.get("claims", [])
        elif r["kind"] == "article":
            for section in r.get("sections", []):
                ai_claims.extend(section.get("claims", []))

        # Merge user claims into the set (avoid duplicates by text)
        ai_texts = {c["text"].lower().strip() for c in ai_claims}
        for uc in user_claims:
            if uc["text"].lower().strip() not in ai_texts:
                ai_claims.append(uc)

        # Enrich each claim with on-chain state
        for claim in ai_claims:
            claim_text = claim["text"]
            on_chain = compute_one(db, claim_text, top_k=5)
            claim["on_chain"] = on_chain
            claim.setdefault("stake_support", 0)
            claim.setdefault("stake_challenge", 0)
            claim.setdefault("verity_score", 0.0)
            claim.setdefault("author", "AI Search")

            post_id = on_chain.get("post_id")
            if post_id is not None:
                try:
                    from chain.chain_reader import get_stake_totals, get_verity_score
                    s, c = get_stake_totals(post_id)
                    claim["stake_support"] = s
                    claim["stake_challenge"] = c
                    claim["verity_score"] = get_verity_score(post_id)
                except Exception:
                    pass

        # Build View 1: topic rows with incumbent/challenger pairing
        from views.topic_view import build_topic_view
        rows = build_topic_view(db, ai_claims, req.input)

        r["topic_rows"] = rows

        return r
    except Exception as e:
        import traceback
        print("Interpret error:", str(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Interpret failed: {str(e)}")


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