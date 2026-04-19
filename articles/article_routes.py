from moderation import check_content_fast
# app/article_routes.py
"""
Article API endpoints.

GET  /api/article/{topic}                    → full article with VS-enriched sentences
POST /api/article/{topic}/generate           → AI-generate + store (idempotent)
POST /api/article/sentence/insert            → add sentence to a section
POST /api/article/sentence/{id}/edit         → replace sentence (create new + challenge old)
POST /api/article/sentence/{id}/register     → register sentence on-chain (lazy)
POST /api/article/sentence/cleanup           → AI grammar cleanup
GET  /api/disambiguate                       → typeahead search
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db
from sqlalchemy import text as sql_text
from rate_limit import ai_rate_limit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["article"])


# ── Request models ──────────────────────────────────────

class GenerateRequest(BaseModel):
    refresh: bool = False

class InsertRequest(BaseModel):
    section_id: int
    after_sentence_id: Optional[int] = None
    text: str

class EditRequest(BaseModel):
    new_text: str

class CleanupRequest(BaseModel):
    text: str
    topic: str = ""

class RegisterRequest(BaseModel):
    """Empty — just triggers on-chain registration."""
    pass


# ── Helpers ─────────────────────────────────────────────

def _enrich_sentences(article: dict) -> dict:
    """Add VS, stake totals to each sentence that has a post_id.
    Also filters blocked content on display."""
    try:
        from chain.chain_reader import get_stake_totals, get_verity_score
    except ImportError:
        return article

    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            pid = sent.get("post_id")
            if pid is not None:
                try:
                    s, c = get_stake_totals(pid)
                    sent["stake_support"] = s
                    sent["stake_challenge"] = c
                    sent["verity_score"] = get_verity_score(pid)
                except Exception:
                    sent["stake_support"] = 0
                    sent["stake_challenge"] = 0
                    sent["verity_score"] = 0
            else:
                sent["stake_support"] = 0
                sent["stake_challenge"] = 0
                sent["verity_score"] = 0

    # Display-time moderation filter
    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            mod = check_content_fast(sent.get("text", ""))
            if not mod.allowed:
                sent["text"] = "[Content hidden — policy violation]"
                sent["moderated"] = True

    # Semantic dedup: remove off-chain near-duplicates
    try:
        _semantic_dedup(article)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Semantic dedup failed: %s", e)

    return article




def _semantic_dedup(article: dict):
    """Remove duplicate and near-duplicate sentences from article display.
    Rules:
      - On-chain sentences (post_id != null): ALWAYS kept, even if near-dupes of each other
      - Off-chain near-duplicate of an on-chain sentence: REMOVED
      - Off-chain near-duplicate of an earlier off-chain sentence: REMOVED (keep first)
    """
    from embedding import embed
    from similarity import cosine_similarity

    DEDUP_THRESHOLD = 0.70  # Cosine similarity above this = near-duplicate

    # First pass: collect and embed all on-chain sentences (across entire article)
    onchain_embeddings = []
    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            if sent.get("post_id") is not None:
                try:
                    vec = embed(sent["text"])
                    onchain_embeddings.append(vec)
                except Exception:
                    pass

    # Second pass: filter each section
    # Track kept off-chain embeddings globally (across sections) to dedup across sections too
    kept_offchain_embeddings = []

    for section in article.get("sections", []):
        filtered = []
        for sent in section.get("sentences", []):
            # Always keep on-chain sentences
            if sent.get("post_id") is not None:
                filtered.append(sent)
                continue

            text = sent.get("text", "").strip()
            if not text:
                continue  # Drop empty sentences

            try:
                vec = embed(text)
            except Exception:
                filtered.append(sent)
                continue

            # Check against on-chain sentences
            is_dupe = False
            for oc_vec in onchain_embeddings:
                if cosine_similarity(vec, oc_vec) >= DEDUP_THRESHOLD:
                    is_dupe = True
                    break

            if is_dupe:
                continue  # Drop: near-dupe of on-chain sentence

            # Check against already-kept off-chain sentences
            for kept_vec in kept_offchain_embeddings:
                if cosine_similarity(vec, kept_vec) >= DEDUP_THRESHOLD:
                    is_dupe = True
                    break

            if is_dupe:
                continue  # Drop: near-dupe of earlier off-chain sentence

            # Keep this sentence
            filtered.append(sent)
            kept_offchain_embeddings.append(vec)

        section["sentences"] = filtered

def _ensure_sentence_in_claim_db(db: Session, text: str) -> int:
    """Ensure a sentence exists in the claim table, return claim_id."""
    from semantic import ensure_claim
    return ensure_claim(db, text)


def _link_unlinked_sentences(db: Session, article: dict):
    """For any article sentence with post_id=NULL, find matching on-chain claims
    using semantic embedding similarity — not just exact text match."""
    from articles.article_store import update_sentence_post_id, invalidate_article_cache
    from semantic import find_best_onchain_match
    patched = 0
    already_used = set()

    # Collect existing post_ids
    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            pid = sent.get("post_id")
            if pid is not None:
                already_used.add(pid)

    for section in article.get("sections", []):
        for sent in section.get("sentences", []):
            if sent.get("post_id") is not None:
                continue
            sid = sent.get("sentence_id")
            text = sent.get("text", "").strip()
            if not sid or not text:
                continue
            try:
                match = find_best_onchain_match(db, text, exclude_post_ids=already_used)
                if match:
                    update_sentence_post_id(db, sid, match["post_id"])
                    invalidate_article_cache(db, sid)
                    sent["post_id"] = match["post_id"]
                    already_used.add(match["post_id"])
                    patched += 1
                    logger.info(
                        "Semantic link: sentence %d -> post %d (sim=%.3f) '%s' <-> '%s'",
                        sid, match["post_id"], match["similarity"],
                        text[:30], match["claim_text"][:30],
                    )
            except Exception as e:
                logger.warning("Failed to semantic-link sentence %d: %s", sid, e)
    if patched:
        logger.info("Patched %d unlinked sentences with on-chain post_ids", patched)




def _increment_view_count(db: Session, article_id: int):
    """Increment the view counter for an article. Non-fatal."""
    try:
        db.execute(sql_text(
            "UPDATE topic_article SET view_count = COALESCE(view_count, 0) + 1 "
            "WHERE article_id = :a"
        ), {"a": article_id})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

# ── Endpoints ───────────────────────────────────────────

@router.get("/article/{topic:path}")
def get_article(topic: str, db: Session = Depends(get_db)):
    """Get a full article. Serves pre-built cached JSON — zero processing."""
    from articles.article_store import ensure_tables
    ensure_tables(db)

    # Try cached response first (one SELECT, zero processing)
    row = db.execute(sql_text(
        "SELECT article_id, cached_response FROM topic_article "
        "WHERE LOWER(topic_key) = LOWER(:t)"
    ), {"t": topic}).fetchone()

    if row and row[1]:
        # Cache hit — serve as-is
        _increment_view_count(db, row[0])
        return row[1]

    # No cache: check if article exists but cache is cold
    from articles.article_store import get_article as load_article
    article = load_article(db, topic)
    if article:
        # Build cache in background; serve enriched version this one time
        import threading
        def _bg_cache(topic_key):
            try:
                from db import get_session_factory
                from articles.article_store import build_and_cache_response
                build_and_cache_response(get_session_factory(), topic_key)
            except Exception as e:
                logger.debug("Background cache build failed: %s", e)
        threading.Thread(target=_bg_cache, args=(topic,), daemon=True).start()

        _increment_view_count(db, article["article_id"])
        return _enrich_sentences(article)

    # No article at all — generate (only slow path: first visit ever)
    return _generate_and_store(topic, db, refresh=False)


@router.get("/article/{topic:path}/version")
def get_article_version(topic: str, db: Session = Depends(get_db)):
    """Return the current article version hash.
    Frontend polls this every 30s to detect updates."""
    row = db.execute(sql_text(
        "SELECT response_hash FROM topic_article WHERE LOWER(topic_key) = LOWER(:t)"
    ), {"t": topic}).fetchone()
    if not row:
        return {"hash": None}
    return {"hash": row[0]}


@router.post("/article/{topic}/generate")
@ai_rate_limit
def generate_article_endpoint(topic: str, req: GenerateRequest,
                              db: Session = Depends(get_db)):
    """Generate (or regenerate) an article for a topic."""
    return _generate_and_store(topic, db, refresh=req.refresh)


def _generate_and_store(topic: str, db: Session, refresh: bool) -> dict:
    print(f"GENERATE_AND_STORE CALLED: topic={topic} refresh={refresh}")
    from articles.article_store import ensure_tables, get_article as load_article, store_article
    from articles.article_gen import generate_article
    ensure_tables(db)

    if not refresh:
        existing = load_article(db, topic)
        if existing:
            return _enrich_sentences(existing)

    logger.info("Generating article for '%s'", topic)
    result = generate_article(topic)
    # Validate generated title matches the requested topic
    gen_title = (result.get("title") or "").lower().strip()
    topic_lower = topic.lower().strip()
    if gen_title and topic_lower not in gen_title and gen_title not in topic_lower:
        logger.warning("Generation rejected: got '%s' for topic '%s', retrying", result.get("title"), topic)
        result = generate_article(topic)  # One retry

    store_article(db, topic, result["title"], result["sections"])
    article = load_article(db, topic)
    if not article:
        raise HTTPException(500, "Failed to load article after storing")

    # Also ensure each sentence is in the claim table for embedding search
    for section in article["sections"]:
        for sent in section["sentences"]:
            try:
                _ensure_sentence_in_claim_db(db, sent["text"])
            except Exception as e:
                logger.warning("Failed to ensure claim for '%s': %s", sent["text"][:50], e)

    # Index existing on-chain claims into this new article
    try:
        from articles.claim_indexer import index_existing_claims_into_article
        index_existing_claims_into_article(db, article["article_id"])
        # Re-load to include any indexed claims
        article = load_article(db, topic)
    except Exception as e:
        logger.warning("Claim indexing failed (non-fatal): %s", e)

    # Run dedup + cache build in background for future instant serving
    import threading
    article_id_for_bg = article["article_id"]
    def _bg_cache(topic_key, art_id):
        try:
            from db import get_session_factory
            from articles.article_store import persist_dedup, build_and_cache_response
            Sess = get_session_factory()
            db = Sess()
            try:
                persist_dedup(db, art_id)
            finally:
                db.close()
            build_and_cache_response(Sess, topic_key)
        except Exception as e:
            logger.debug("Post-generate dedup+cache build failed: %s", e)
    threading.Thread(target=_bg_cache, args=(topic, article_id_for_bg), daemon=True).start()

    return _enrich_sentences(article)


@router.post("/article/sentence/insert")
def insert_sentence_endpoint(req: InsertRequest, db: Session = Depends(get_db)):
    """Insert a new sentence into a section."""
    from articles.article_store import ensure_tables, insert_sentence
    from articles.article_gen import split_into_sentences
    ensure_tables(db)

    sentences = split_into_sentences(req.text)
    inserted = []

    after_id = req.after_sentence_id
    for sent_text in sentences:
        sid = insert_sentence(db, req.section_id, after_id, sent_text)

        # Ensure in claim DB
        try:
            _ensure_sentence_in_claim_db(db, sent_text)
        except Exception:
            pass

        # Check if already on chain
        from sqlalchemy import text as sql_text
        existing_post = db.execute(sql_text(
            "SELECT post_id FROM claim WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) "
            "AND post_id IS NOT NULL LIMIT 1"
        ), {"t": sent_text}).fetchone()
        existing_pid = existing_post[0] if existing_post else None
        if existing_pid:
            from articles.article_store import update_sentence_post_id
            update_sentence_post_id(db, sid, existing_pid)
            invalidate_article_cache(db, sid)

        inserted.append({"sentence_id": sid, "text": sent_text, "post_id": existing_pid})
        after_id = sid  # Chain insertions

    return {"inserted": inserted}


@router.post("/article/sentence/{sentence_id}/edit")
def edit_sentence_endpoint(sentence_id: int, req: EditRequest,
                           db: Session = Depends(get_db)):
    """Replace a sentence: creates new sentence(s) + marks old as replaced.

    The frontend is responsible for creating the on-chain challenge link
    (new_post_id challenges old_post_id) since that requires the user's wallet.
    """
    from articles.article_store import (
        ensure_tables, insert_sentence, mark_replaced,
    )
    from articles.article_gen import split_into_sentences
    from sqlalchemy import text as sql_text
    ensure_tables(db)

    # Get the old sentence's section_id
    old = db.execute(sql_text(
        "SELECT section_id, sort_order, text, post_id FROM article_sentence "
        "WHERE sentence_id = :id"
    ), {"id": sentence_id}).fetchone()
    if not old:
        raise HTTPException(404, "Sentence not found")

    section_id, old_order, old_text, old_post_id = old

    # Insert as a single sentence — claims are atomic
    new_sentences = [req.new_text.strip()]
    created = []

    # Re-evaluate section placement if text changed significantly
    target_section_id = section_id
    try:
        from embedding import embed
        from similarity import cosine_similarity
        old_vec = embed(old_text)
        new_vec = embed(req.new_text.strip())
        sim = cosine_similarity(old_vec, new_vec)
        if sim < 0.85:  # Text changed significantly
            from articles.claim_indexer import find_best_section
            # Get article_id from section
            art_row = db.execute(sql_text(
                "SELECT article_id FROM article_section WHERE section_id = :s"
            ), {"s": section_id}).fetchone()
            if art_row:
                better_section = find_best_section(db, art_row[0], req.new_text.strip())
                if better_section and better_section != section_id:
                    target_section_id = better_section
                    logger.info("Edit moved to different section: %d -> %d (sim=%.3f)",
                                section_id, better_section, sim)
    except Exception as e:
        logger.debug("Section re-evaluation failed (keeping original): %s", e)

    after_id = sentence_id if target_section_id == section_id else None
    for sent_text in new_sentences:
        new_sid = insert_sentence(db, target_section_id, after_id, sent_text)
        try:
            _ensure_sentence_in_claim_db(db, sent_text)
        except Exception:
            pass

        # Check if this claim already exists on-chain
        existing_post = db.execute(sql_text(
            "SELECT post_id FROM claim WHERE LOWER(TRIM(claim_text)) = LOWER(TRIM(:t)) "
            "AND post_id IS NOT NULL LIMIT 1"
        ), {"t": sent_text}).fetchone()
        existing_pid = existing_post[0] if existing_post else None
        if existing_pid:
            from articles.article_store import update_sentence_post_id
            update_sentence_post_id(db, new_sid, existing_pid)
            invalidate_article_cache(db, new_sid)

        created.append({
            "sentence_id": new_sid,
            "text": sent_text,
            "post_id": existing_pid,  # Frontend can skip createClaim if already on chain
        })
        after_id = new_sid

    # Mark old as replaced by the first new sentence
    if created:
        mark_replaced(db, sentence_id, created[0]["sentence_id"])

    return {
        "old_sentence_id": sentence_id,
        "old_post_id": old_post_id,
        "created": created,
    }


@router.post("/article/sentence/{sentence_id}/register")
def register_sentence_endpoint(sentence_id: int,
                               db: Session = Depends(get_db)):
    """Register a sentence on-chain. Called when a user first interacts with it.

    Note: actual on-chain tx is done client-side via wallet. This endpoint
    just links the sentence to its post_id after the client reports it.
    """
    # This is actually handled by a separate call — the client creates the
    # claim on-chain and then calls this to update the DB.
    # See: POST /api/article/sentence/{id}/link_post
    raise HTTPException(501,
        "Use /api/article/sentence/{id}/link_post with {post_id} instead")


class LinkPostRequest(BaseModel):
    post_id: int

@router.post("/article/sentence/{sentence_id}/link_post")
def link_post_endpoint(sentence_id: int, req: LinkPostRequest,
                       db: Session = Depends(get_db)):
    """Link a sentence to its on-chain post_id after client-side registration."""
    from articles.article_store import ensure_tables, update_sentence_post_id
    ensure_tables(db)
    update_sentence_post_id(db, sentence_id, req.post_id)
    invalidate_article_cache(db, sentence_id)

    # Rebuild article cache so the linked post_id appears immediately
    try:
        topic_row = db.execute(sql_text(
            "SELECT ta.topic_key FROM topic_article ta "
            "JOIN article_sentence s ON s.article_id = ta.article_id "
            "WHERE s.sentence_id = :sid"
        ), {"sid": sentence_id}).fetchone()
        if topic_row:
            import threading
            def _rebuild(tk):
                try:
                    from db import get_session_factory
                    from articles.article_store import build_and_cache_response
                    build_and_cache_response(get_session_factory(), tk)
                except Exception:
                    pass
            threading.Thread(target=_rebuild, args=(topic_row[0],), daemon=True).start()
    except Exception:
        pass

    return {"sentence_id": sentence_id, "post_id": req.post_id}


@router.post("/article/sentence/cleanup")
@ai_rate_limit
def cleanup_sentence_endpoint(req: CleanupRequest):
    """AI grammar/spelling cleanup. Returns original + suggested."""
    from articles.article_gen import cleanup_sentence
    original = req.text.strip()
    suggested = cleanup_sentence(original, topic=req.topic)
    return {"original": original, "suggested": suggested}


@router.get("/disambiguate")
def disambiguate_endpoint(q: str, db: Session = Depends(get_db)):
    """Typeahead disambiguation for search bar."""
    if not q or len(q.strip()) < 1:
        return {"results": []}
    from articles.article_store import ensure_tables, disambiguate
    ensure_tables(db)
    return {"results": disambiguate(db, q.strip())}


@router.get("/claims/{post_id}/stakes")
def get_user_stakes(post_id: int, user: str = None):
    """Get stake totals and user-specific stakes for a post_id."""
    try:
        from chain.chain_reader import get_stake_totals, get_user_stake, get_verity_score
        support, challenge = get_stake_totals(post_id)
        result = {
            "post_id": post_id,
            "stake_support": support,
            "stake_challenge": challenge,
            "verity_score": get_verity_score(post_id),
            "user_support": 0,
            "user_challenge": 0,
        }
        if user:
            result["user_support"] = get_user_stake(user, post_id, 0)
            result["user_challenge"] = get_user_stake(user, post_id, 1)
        return result
    except Exception as e:
        logger.warning("Failed to read stakes for post %d: %s", post_id, e)
        return {"post_id": post_id, "stake_support": 0, "stake_challenge": 0,
                "verity_score": 0, "user_support": 0, "user_challenge": 0}

@router.get("/topics/popular")
def popular_topics(limit: int = 8, db: Session = Depends(get_db)):
    """Return the most-viewed topics for the landing page."""
    from sqlalchemy import text as sql_text
    rows = db.execute(sql_text(
        "SELECT topic_key, title, view_count FROM topic_article "
        "ORDER BY COALESCE(view_count, 0) DESC, title ASC LIMIT :l"
    ), {"l": limit}).fetchall()

    return {"topics": [
        {"key": r[0], "title": r[1], "views": r[2]}
        for r in rows
    ]}



class DetectTopicRequest(BaseModel):
    claim_text: str
    post_id: int

@router.post("/claims/detect-topic")
def detect_topic_endpoint(req: DetectTopicRequest, db: Session = Depends(get_db)):
    """Auto-detect topic for a standalone claim, store the association,
    and trigger background article generation if needed.
    Returns immediately with the detected topic."""
    from articles.topic_detect import detect_topic, ensure_article_for_claim

    topic = detect_topic(req.claim_text)
    if not topic:
        return {"topic": None, "status": "detection_failed"}

    # Store topic association in the claim table
    try:
        db.execute(sql_text(
            "UPDATE claim SET topic = :t WHERE post_id = :pid"
        ), {"t": topic, "pid": req.post_id})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    # Ensure article exists (generates in background if not)
    try:
        ensure_article_for_claim(db, req.claim_text, req.post_id, topic)
    except Exception as e:
        logger.warning("ensure_article_for_claim failed: %s", e)

    return {"topic": topic, "status": "ok"}



@router.post("/moderate")
def moderate_endpoint(req: CleanupRequest):
    """Check if content passes moderation. Returns {allowed, reason}."""
    from moderation import check_content
    result = check_content(req.text)
    return {"allowed": result.allowed, "reason": result.reason}



@router.post("/article/{topic:path}/refresh")
def refresh_article_endpoint(topic: str, db: Session = Depends(get_db)):
    """On-demand article refresh. Generates new content and merges with existing.
    Preserves all existing sentences and their on-chain claim links."""
    from articles.article_store import refresh_article, build_and_cache_response
    try:
        added = refresh_article(db, topic)
        # Rebuild cached response with new content
        import threading
        def _bg_cache(t):
            try:
                from db import get_session_factory
                build_and_cache_response(get_session_factory(), t)
            except Exception:
                pass
        threading.Thread(target=_bg_cache, args=(topic,), daemon=True).start()
        return {"refreshed": added, "topic": topic}
    except Exception as e:
        logger.warning("Article refresh failed for '%s': %s", topic, e)
        raise HTTPException(500, f"Refresh failed: {e}")

