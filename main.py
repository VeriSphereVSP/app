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
from semantic_dedup import router as semantic_dedup_router
from rate_limit import RateLimitMiddleware, cleanup_rate_limiter


@asynccontextmanager
async def lifespan(app):
    # Background tasks (indexer, article refresh, dupe groups) run in
    # the separate worker service — see worker.py and docker-compose.yml.
    print("API server started (background tasks run in worker service)")
    # Periodic rate limiter cleanup
    import asyncio as _aio
    async def _rl_cleanup():
        while True:
            await _aio.sleep(600)
            cleanup_rate_limiter()
    _aio.create_task(_rl_cleanup())

    # Dupe refresh runs in worker service

    # Background article refresh — autotunes interval to spread load over 24h
    async def _daily_refresh():
        import asyncio, statistics, time
        from db import get_session_factory
        from articles.article_store import refresh_article, persist_dedup, build_and_cache_response
        from sqlalchemy import text as sql_text
        CYCLE_SECONDS = 86400  # 24h target
        recent_elapsed = []
        # Initial sleep to avoid hitting LLM during app startup
        await asyncio.sleep(120)
        while True:
            try:
                Sess = get_session_factory()
                # Pick the article with the oldest last_refreshed_at
                db = Sess()
                try:
                    row = db.execute(sql_text(
                        "SELECT topic_key, article_id FROM topic_article "
                        "ORDER BY last_refreshed_at NULLS FIRST LIMIT 1"
                    )).fetchone()
                    n_row = db.execute(sql_text("SELECT COUNT(*) FROM topic_article")).fetchone()
                    N = n_row[0] if n_row else 1
                finally:
                    db.close()
                
                if row:
                    topic_key, article_id = row
                    t0 = time.time()
                    
                    def _do_refresh():
                        sess = Sess()
                        try:
                            refresh_article(sess, topic_key)
                        finally:
                            sess.close()
                        sess = Sess()
                        try:
                            persist_dedup(sess, article_id)
                        finally:
                            sess.close()
                        build_and_cache_response(Sess, topic_key)
                    
                    try:
                        await asyncio.to_thread(_do_refresh)
                        elapsed = time.time() - t0
                        recent_elapsed.append(elapsed)
                        if len(recent_elapsed) > 10:
                            recent_elapsed.pop(0)
                        avg = statistics.mean(recent_elapsed)
                        print(f"bg-refresh: '{topic_key}' done in {elapsed:.1f}s (avg {avg:.1f}s, N={N})")
                    except Exception as e:
                        print(f"bg-refresh failed for '{topic_key}': {e}")
                
                # Autotune: spread CYCLE_SECONDS evenly across N articles
                avg = statistics.mean(recent_elapsed) if recent_elapsed else 30.0
                sleep_s = max(60, (CYCLE_SECONDS - N * avg) / max(N, 1))
                print(f"bg-refresh: sleeping {sleep_s:.0f}s before next (N={N}, avg={avg:.1f}s)")
                await asyncio.sleep(sleep_s)
            except Exception as e:
                print(f"bg-refresh loop error: {e}")
                await asyncio.sleep(120)
    _aio.create_task(_daily_refresh())

    yield
    print("API server stopped")


from chain_indexer import start_indexer

app = FastAPI(title="VeriSphere App API", version="0.1.0", lifespan=lifespan)


app.add_middleware(RateLimitMiddleware)

app.include_router(relay_router)
app.include_router(supersedes_router)
app.include_router(mm_router)
app.include_router(claim_views_router)
app.include_router(portfolio_router)
app.include_router(article_router)
app.include_router(semantic_dedup_router)

ADDRESSES_PATH = Path("/app/broadcast/Deploy.s.sol/43113/addresses.json")



# ── Admin auth (OPS-03: audit logging + IP allowlist) ──────────────────────────
import os as _os
ADMIN_API_KEY = _os.getenv("ADMIN_API_KEY", "")
# Comma-separated list of allowed IPs. Empty = allow all (but still require key).
ADMIN_IP_ALLOWLIST = [ip.strip() for ip in _os.getenv("ADMIN_IP_ALLOWLIST", "").split(",") if ip.strip()]


def _get_client_ip(request) -> str:
    if not request:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _log_admin_action(db, action: str, params: dict, request):
    """Write to admin_audit_log table."""
    try:
        ip = _get_client_ip(request)
        key = request.headers.get("X-Admin-Key", "")[:8] if request else ""
        db.execute(sql_text(
            "INSERT INTO admin_audit_log (action, params, ip_address, admin_key_prefix) "
            "VALUES (:a, CAST(:p AS jsonb), :ip, :kp)"
        ), {"a": action, "p": json.dumps(params), "ip": ip, "kp": key})
        db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Admin audit log failed: %s", e)


def require_admin(request, db=None, action=None, params=None):
    """Check X-Admin-Key header, enforce IP allowlist, log action."""
    if not ADMIN_API_KEY:
        raise HTTPException(403, "Admin API key not configured. Set ADMIN_API_KEY env var.")

    # IP allowlist check
    if ADMIN_IP_ALLOWLIST:
        ip = _get_client_ip(request)
        if ip not in ADMIN_IP_ALLOWLIST and ip != "127.0.0.1":
            raise HTTPException(403, "Admin access denied from this IP.")

    key = request.headers.get("X-Admin-Key", "")
    if key != ADMIN_API_KEY:
        raise HTTPException(403, "Invalid admin key")

    # Audit log
    if db and action:
        _log_admin_action(db, action, params or {}, request)

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
    require_admin(request, db=db, action="update_cost", params={"cost_key": cost_key, "monthly_usd": monthly_usd})
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
    require_admin(request, db=db, action="update_fee_param", params={"param_key": param_key, "value": value})
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
            from chain.chain_db import get_stake_totals as db_stakes, get_verity_score as db_vs, get_user_stake as db_user
            support, challenge = db_stakes(db, post_id)
            result["stake_support"] = support
            result["stake_challenge"] = challenge
            result["verity_score"] = db_vs(db, post_id)

            if user:
                result["user_support"] = db_user(db, user, post_id, 0)
                result["user_challenge"] = db_user(db, user, post_id, 1)
        except Exception as e:
            import traceback
            print(f"Failed to read state for post_id={post_id}: {e}")
            print(traceback.format_exc())

    return result


@app.get("/api/claims/{post_id}/user-stake")
def get_user_stake_endpoint(post_id: int, user: str = None, db: Session = Depends(get_db)):
    """Get user's stake on a specific post by post_id."""
    result = {"user_support": 0, "user_challenge": 0}
    if not user:
        return result
    try:
        from chain.chain_db import get_user_stake as db_user
        result["user_support"] = db_user(db, user, post_id, 0)
        result["user_challenge"] = db_user(db, user, post_id, 1)
    except Exception as e:
        print(f"Failed to read user stake for post_id={post_id}, user={user}: {e}")
    return result



@app.post("/api/user-stakes")
def get_user_stakes_batch(body: dict, db: Session = Depends(get_db)):
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
        from chain.chain_db import get_user_stake as db_user
        for pid in post_ids:
            try:
                stakes[str(pid)] = {
                    "user_support": db_user(db, user, pid, 0),
                    "user_challenge": db_user(db, user, pid, 1),
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
    """Trigger immediate reindex of a post, user stakes, and invalidate article cache."""
    try:
        from chain_indexer import index_post
        from db import get_session_factory
        from sqlalchemy import text as sql_text
        db = get_session_factory()()
        users = [user] if user else None
        index_post(db, post_id, user_addresses=users)
        # Note: link_unlinked_sentences runs during build_and_cache_response anyway,
        # so we don't need to do it here. Just invalidate the cache.
        # Invalidate article caches that reference this post
        db.execute(sql_text(
            "UPDATE topic_article SET cached_response = NULL "
            "WHERE article_id IN ("
            "  SELECT DISTINCT sec.article_id FROM article_section sec "
            "  JOIN article_sentence s ON s.section_id = sec.section_id "
            "  WHERE s.post_id = :pid"
            ")"
        ), {"pid": post_id})
        db.commit()
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