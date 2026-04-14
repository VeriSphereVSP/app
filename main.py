# app/main.py
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from lang_detect import detect_language, lang_instruction, is_rtl
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional
from datetime import datetime
from pathlib import Path
import json

from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy import text as sql_text

from db import get_db
from config import USDC_ADDRESS, VSP_ADDRESS, FORWARDER_ADDRESS
from semantic import compute_one
from chain.claim_registry import create_claim
from chain.stake import stake_claim
from relay import router as relay_router
from supersedes import router as supersedes_router
from mm.mm_routes import router as mm_router
from claim_views import router as claim_views_router
from portfolio_views import router as portfolio_router
from articles.article_routes import router as article_router
from rate_limit import RateLimitMiddleware, cleanup_rate_limiter


@asynccontextmanager
async def lifespan(app):
    from chain.indexer import run_indexer
    from chain_indexer import start_indexer
    indexer_task = asyncio.create_task(run_indexer())
    start_indexer()
    print("Blockchain event indexer started")
    # Periodic rate limiter cleanup
    import asyncio as _aio
    async def _rl_cleanup():
        while True:
            await _aio.sleep(600)
            cleanup_rate_limiter()
    _aio.create_task(_rl_cleanup())

    # Daily article refresh — refreshes stale articles in the background
    async def _daily_refresh():
        import asyncio
        while True:
            await asyncio.sleep(900)  # Safety net: 15 min (chain events trigger immediate rebuilds)
            try:
                from db import get_session_factory
                from articles.article_store import refresh_article
                from sqlalchemy import text as sql_text
                session = get_session_factory()()
                try:
                    # Find articles not refreshed in 24h, ordered by view_count
                    stale = session.execute(sql_text(
                        "SELECT topic_key FROM topic_article "
                        "WHERE last_refreshed_at IS NULL "
                        "   OR last_refreshed_at < NOW() - INTERVAL '24 hours' "
                        "ORDER BY COALESCE(view_count, 0) DESC LIMIT 5"
                    )).fetchall()
                    for (topic_key,) in stale:
                        try:
                            refresh_article(session, topic_key)
                        except Exception as e:
                            print(f"Daily refresh failed for '{topic_key}': {e}")
                        await asyncio.sleep(10)  # Don't hammer the LLM
                finally:
                    session.close()
            except Exception as e:
                print(f"Daily refresh loop error: {e}")
    _aio.create_task(_daily_refresh())

    yield
    indexer_task.cancel()
    try:
        await indexer_task
    except asyncio.CancelledError:
        pass
    print("Blockchain event indexer stopped")


from chain_indexer import start_indexer

app = FastAPI(title="VeriSphere App API", version="0.1.0", lifespan=lifespan)


app.add_middleware(RateLimitMiddleware)

app.include_router(relay_router)
app.include_router(supersedes_router)
app.include_router(mm_router)
app.include_router(claim_views_router)
app.include_router(portfolio_router)
app.include_router(article_router)

ADDRESSES_PATH = Path("/app/broadcast/Deploy.s.sol/43113/addresses.json")



# ── Admin auth ─────────────────────────────────────────────────────────────────
import os as _os
ADMIN_API_KEY = _os.getenv("ADMIN_API_KEY", "")

def require_admin(request):
    """Check X-Admin-Key header against ADMIN_API_KEY env var."""
    if not ADMIN_API_KEY:
        raise HTTPException(403, "Admin API key not configured. Set ADMIN_API_KEY env var.")
    key = request.headers.get("X-Admin-Key", "")
    if key != ADMIN_API_KEY:
        raise HTTPException(403, "Invalid admin key")

@app.get("/healthz")
def healthz():
    return {"ok": "true"}



@app.get("/api/fees")
def get_fees(db: Session = Depends(get_db)):
    """Full fee schedule with cost breakdown and examples."""
    from fee_calculator import get_fee_schedule
    return get_fee_schedule(db)

@app.get("/api/fees/estimate")
def estimate_fee(tx_type: str, value_vsp: float = 1.0, db: Session = Depends(get_db)):
    """Estimate fee for a specific transaction type and value."""
    from fee_calculator import compute_fee
    return compute_fee(db, tx_type, value_vsp)

@app.post("/api/fees/costs")
def update_cost(cost_key: str, monthly_usd: float, request: Request = None, db: Session = Depends(get_db)):
    require_admin(request)
    """Update an operating cost (admin). Fee recalculates automatically."""
    from fee_calculator import invalidate_cache
    db.execute(sql_text(
        "UPDATE operating_costs SET monthly_usd = :usd, updated_at = NOW() WHERE cost_key = :key"
    ), {"key": cost_key, "usd": monthly_usd})
    db.commit()
    invalidate_cache()
    return {"ok": True}

@app.post("/api/fees/params")
def update_fee_param(param_key: str, value: str, request: Request = None, db: Session = Depends(get_db)):
    require_admin(request)
    """Update a fee parameter (admin). Fee recalculates automatically."""
    from fee_calculator import invalidate_cache
    db.execute(sql_text(
        "UPDATE fee_params SET value = :val, updated_at = NOW() WHERE param_key = :key"
    ), {"key": param_key, "val": value})
    db.commit()
    invalidate_cache()
    return {"ok": True}

@app.get("/api/contracts")
def get_contracts():
    if not ADDRESSES_PATH.exists():
        raise HTTPException(500, f"Deployment artifact not found at {ADDRESSES_PATH}")
    try:
        with ADDRESSES_PATH.open() as f:
            contracts = json.load(f)
        contracts["USDC"] = USDC_ADDRESS
        contracts["Forwarder"] = FORWARDER_ADDRESS
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


@app.get("/api/claims/{post_id}/user-stake")
def get_user_stake_endpoint(post_id: int, user: str = None):
    """Get user's stake on a specific post by post_id."""
    result = {"user_support": 0, "user_challenge": 0}
    if not user:
        return result
    try:
        from chain.chain_reader import get_user_stake
        result["user_support"] = get_user_stake(user, post_id, 0)
        result["user_challenge"] = get_user_stake(user, post_id, 1)
    except Exception as e:
        print(f"Failed to read user stake for post_id={post_id}, user={user}: {e}")
    return result



@app.post("/api/user-stakes")
def get_user_stakes_batch(body: dict):
    """Get user's stake on multiple posts in a single request.
    
    Body: {"user": "0x...", "post_ids": [1, 2, 3, ...]}
    Returns: {"stakes": {"1": {user_support: ..., user_challenge: ...}, ...}}
    """
    user = body.get("user")
    post_ids = body.get("post_ids", [])
    stakes = {}
    if not user or not post_ids:
        return {"stakes": stakes}
    try:
        from chain.chain_reader import get_user_stake
        for pid in post_ids:
            try:
                stakes[str(pid)] = {
                    "user_support": get_user_stake(user, pid, 0),
                    "user_challenge": get_user_stake(user, pid, 1),
                }
            except Exception:
                stakes[str(pid)] = {"user_support": 0, "user_challenge": 0}
    except Exception as e:
        print(f"Batch user-stakes failed: {e}")
    return {"stakes": stakes}

@app.get("/api/claims/{post_id}/debug")
def debug_claim(post_id: int):
    """Debug: show raw on-chain data for a claim to verify VS calculation."""
    from chain.chain_reader import get_stake_totals, get_verity_score, _get_score_engine
    result = {}
    try:
        support, challenge = get_stake_totals(post_id)
        result["stake_support"] = support
        result["stake_challenge"] = challenge
        result["stake_total"] = support + challenge
        if support + challenge > 0:
            result["simple_vs"] = ((support - challenge) / (support + challenge)) * 100
        else:
            result["simple_vs"] = 0
    except Exception as e:
        result["stake_error"] = str(e)
    try:
        se = _get_score_engine()
        vs_ray = se.functions.effectiveVSRay(post_id).call()
        result["effectiveVSRay_raw"] = str(vs_ray)
        result["effectiveVS_pct"] = (vs_ray / 1e18) * 100
    except Exception as e:
        result["vs_ray_error"] = str(e)
    result["get_verity_score_result"] = get_verity_score(post_id)
    return result


# Old /api/interpret endpoint removed — replaced by /api/article/{topic}
# Old /api/disambiguate endpoint removed — now in article_routes.py


class CreateClaimRequest(BaseModel):
    text: str = Field(..., min_length=3)


@app.post("/api/claims/create")
def create_claim_endpoint(req: CreateClaimRequest):
    try:
        tx_hash = create_claim(req.text)
        return {"tx_hash": tx_hash}
    except Exception as e:
        raise HTTPException(500, f"Failed to create claim: {str(e)}")


class RecordClaimRequest(BaseModel):
    text: str = Field(..., min_length=1)
    post_id: int = Field(..., ge=0)


@app.post("/api/claims/record")
def record_claim_endpoint(req: RecordClaimRequest, db: Session = Depends(get_db)):
    """Record a claim's on-chain post_id in the local DB.
    Called by the frontend after a successful on-chain creation."""
    try:
        db.execute(sql_text(
            "UPDATE claim SET post_id = :pid "
            "WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
        ), {"pid": req.post_id, "t": req.text})
        db.commit()
        return {"ok": True, "post_id": req.post_id}
    except Exception as e:
        print(f"record_claim failed: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/api/claims/check-onchain")
def check_claim_onchain(text: str, db: Session = Depends(get_db)):
    """Check if a claim already exists on-chain. Returns post_id if it does.
    Also syncs the local DB if a match is found."""
    from chain.check_duplicate import check_claim_exists_onchain
    result = check_claim_exists_onchain(text)
    if result and result.get("post_id") is not None:
        post_id = result["post_id"]
        # Sync local DB
        try:
            db.execute(sql_text(
                "UPDATE claim SET post_id = :pid "
                "WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
            ), {"pid": post_id, "t": text})
            db.execute(sql_text(
                "UPDATE article_sentence SET post_id = :pid "
                "WHERE LOWER(TRIM(text)) = LOWER(TRIM(:t)) AND post_id IS NULL"
            ), {"pid": post_id, "t": text})
            db.commit()
        except Exception as e:
            print(f"check-onchain DB sync failed: {e}")
        return {"exists": True, "post_id": post_id}
    return {"exists": False, "post_id": None}



class StakeRequest(BaseModel):
    claim_id: int = Field(..., ge=0)
    side: str = Field(..., pattern="^(support|challenge)$")
    amount: int = Field(..., gt=0)




# ── Token read endpoints (replaces direct chain reads from frontend) ──────────

@app.get("/api/token/allowance")
def token_allowance(owner: str, spender: str):
    """Read VSP token allowance. Frontend calls this instead of readContract."""
    from mm_wallet import w3
    from web3 import Web3
    from chain.abi import VSP_TOKEN_ABI
    from config import VSP_TOKEN_ADDRESS
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=VSP_TOKEN_ABI,
        )
        val = token.functions.allowance(
            Web3.to_checksum_address(owner),
            Web3.to_checksum_address(spender),
        ).call()
        return {"allowance": str(val)}
    except Exception as e:
        print(f"token/allowance failed: {e}")
        return {"allowance": "0"}


@app.get("/api/token/balance")
def token_balance(address: str):
    """Read VSP token balance. Frontend calls this instead of readContract."""
    from mm_wallet import w3
    from web3 import Web3
    from chain.abi import VSP_TOKEN_ABI
    from config import VSP_TOKEN_ADDRESS
    try:
        token = w3.eth.contract(
            address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS),
            abi=VSP_TOKEN_ABI,
        )
        val = token.functions.balanceOf(
            Web3.to_checksum_address(address),
        ).call()
        return {"balance": str(val)}
    except Exception as e:
        print(f"token/balance failed: {e}")
        return {"balance": "0"}




@app.post("/api/reindex/{post_id}")
def reindex_post(post_id: int, user: str = None):
    """Trigger immediate reindex of a post and optionally a user's stakes."""
    try:
        from chain_indexer import index_post
        from db import get_session_factory
        db = get_session_factory()()
        users = [user] if user else None
        index_post(db, post_id, user_addresses=users)
        db.close()
        return {"ok": True, "post_id": post_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

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