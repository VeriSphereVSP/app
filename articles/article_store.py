# app/article_store.py
"""
Article storage: topic_article → article_section → article_sentence.

Every sentence is a stakeable claim. Sentences start off-chain (post_id=NULL)
and get registered on-chain on first interaction.
"""
import json
import logging
import re
import unicodedata
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)


def _norm(topic: str) -> str:
    t = unicodedata.normalize("NFC", topic.strip().lower())
    return re.sub(r"\s+", " ", t)


# ── Schema ──────────────────────────────────────────────

def ensure_tables(db: Session):
    # Migrate: add is_hidden for dedup-persisted sentences
    try:
        db.execute(sql_text(
            "ALTER TABLE article_sentence ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN NOT NULL DEFAULT FALSE"
        ))
    except Exception:
        pass
    # Migrate: add embedding column for cached sentence embeddings
    try:
        db.execute(sql_text(
            "ALTER TABLE article_sentence ADD COLUMN IF NOT EXISTS embedding JSONB"
        ))
    except Exception:
        pass
    # Migrate: add cached_response + response_hash columns if missing
    try:
        from sqlalchemy import text as sql_text
        db.execute(sql_text(
            "ALTER TABLE topic_article "
            "ADD COLUMN IF NOT EXISTS cached_response JSONB"
        ))
        db.execute(sql_text(
            "ALTER TABLE topic_article "
            "ADD COLUMN IF NOT EXISTS response_hash VARCHAR(16)"
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    """No-op. Schema is now managed by ops/compose/migrations/040_article_tables.sql."""
    pass


# ── Reads ───────────────────────────────────────────────

def get_article(db: Session, topic: str) -> Optional[Dict[str, Any]]:
    """Load a full article with sections and sentences."""
    key = _norm(topic)
    art = db.execute(
        sql_text("SELECT article_id, title FROM topic_article WHERE topic_key = :k"),
        {"k": key},
    ).fetchone()
    if not art:
        return None

    article_id, title = art
    secs = db.execute(sql_text(
        "SELECT section_id, heading, sort_order "
        "FROM article_section WHERE article_id = :a ORDER BY sort_order"
    ), {"a": article_id}).fetchall()

    sections = []
    for sec_id, heading, _ in secs:
        sents = db.execute(sql_text(
            "SELECT sentence_id, sort_order, text, post_id, replaced_by "
            "FROM article_sentence WHERE section_id = :s AND is_hidden = FALSE ORDER BY sort_order"
        ), {"s": sec_id}).fetchall()

        sentences = []
        for sid, so, txt, pid, repl in sents:
            sentences.append({
                "sentence_id": sid,
                "sort_order": so,
                "text": txt,
                "post_id": pid,
                "replaced_by": repl,
            })
        sections.append({
            "section_id": sec_id,
            "heading": heading,
            "sentences": sentences,
        })

    return {"article_id": article_id, "title": title, "topic_key": key, "sections": sections}


def disambiguate(db: Session, prefix: str, limit: int = 8) -> List[Dict[str, str]]:
    key = _norm(prefix)
    if not key:
        return []
    rows = db.execute(sql_text(
        "SELECT topic_key, title FROM topic_article "
        "WHERE topic_key LIKE :p ORDER BY updated_at DESC LIMIT :l"
    ), {"p": key + "%", "l": limit}).fetchall()
    results = [{"key": r[0], "title": r[1], "source": "cached"} for r in rows]

    if len(results) < limit:
        remaining = limit - len(results)
        seen = {r["key"] for r in results}
        claims = db.execute(sql_text(
            "SELECT DISTINCT claim_text FROM claim "
            "WHERE post_id IS NOT NULL AND LOWER(claim_text) LIKE :p "
            "ORDER BY claim_text LIMIT :l"
        ), {"p": "%" + key + "%", "l": remaining}).fetchall()
        for r in claims:
            k = _norm(r[0])
            if k not in seen:
                results.append({"key": k, "title": r[0], "source": "claim"})
                seen.add(k)
    return results


# ── Writes ──────────────────────────────────────────────

def store_article(db: Session, topic: str, title: str,
                  sections: List[Dict[str, Any]]) -> int:
    """Store a full article from AI generation. Returns article_id."""
    key = _norm(topic)

    # Upsert article
    existing = db.execute(
        sql_text("SELECT article_id FROM topic_article WHERE topic_key = :k"),
        {"k": key},
    ).fetchone()

    if existing:
        article_id = existing[0]
        db.execute(sql_text(
            "UPDATE topic_article SET title = :t, updated_at = NOW() WHERE article_id = :a"
        ), {"t": title, "a": article_id})
        # Delete old sections + sentences
        old_secs = db.execute(sql_text(
            "SELECT section_id FROM article_section WHERE article_id = :a"
        ), {"a": article_id}).fetchall()
        for (sid,) in old_secs:
            db.execute(sql_text("DELETE FROM article_sentence WHERE section_id = :s"), {"s": sid})
        db.execute(sql_text("DELETE FROM article_section WHERE article_id = :a"), {"a": article_id})
    else:
        row = db.execute(sql_text(
            "INSERT INTO topic_article (topic_key, title) VALUES (:k, :t) RETURNING article_id"
        ), {"k": key, "t": title}).fetchone()
        article_id = row[0]

    # Insert sections and sentences
    for si, sec in enumerate(sections):
        row = db.execute(sql_text(
            "INSERT INTO article_section (article_id, heading, sort_order) "
            "VALUES (:a, :h, :so) RETURNING section_id"
        ), {"a": article_id, "h": sec.get("heading", ""), "so": si * 100}).fetchone()
        section_id = row[0]

        for sj, sent_text in enumerate(sec.get("sentences", [])):
            db.execute(sql_text(
                "INSERT INTO article_sentence (section_id, sort_order, text) "
                "VALUES (:s, :so, :t)"
            ), {"s": section_id, "so": sj * 100, "t": sent_text})

    db.commit()
    logger.info("Stored article '%s' (%d sections)", key, len(sections))
    return article_id


def insert_sentence(db: Session, section_id: int,
                    after_sentence_id: Optional[int], text: str) -> int:
    """Insert a new sentence into a section. Returns sentence_id."""
    if after_sentence_id:
        # Get the sort_order of the sentence we're inserting after
        row = db.execute(sql_text(
            "SELECT sort_order FROM article_sentence WHERE sentence_id = :id"
        ), {"id": after_sentence_id}).fetchone()
        if not row:
            raise ValueError(f"Sentence {after_sentence_id} not found")
        after_order = row[0]

        # Get the next sentence's sort_order
        nxt = db.execute(sql_text(
            "SELECT MIN(sort_order) FROM article_sentence "
            "WHERE section_id = :s AND sort_order > :o"
        ), {"s": section_id, "o": after_order}).fetchone()
        next_order = nxt[0] if nxt and nxt[0] is not None else after_order + 100
        new_order = (after_order + next_order) // 2
        if new_order == after_order:
            # Rebalance
            _rebalance_sort_orders(db, section_id)
            return insert_sentence(db, section_id, after_sentence_id, text)
    else:
        # Insert at the beginning
        first = db.execute(sql_text(
            "SELECT MIN(sort_order) FROM article_sentence WHERE section_id = :s"
        ), {"s": section_id}).fetchone()
        first_order = first[0] if first and first[0] is not None else 100
        new_order = first_order - 100

    row = db.execute(sql_text(
        "INSERT INTO article_sentence (section_id, sort_order, text) "
        "VALUES (:s, :so, :t) RETURNING sentence_id"
    ), {"s": section_id, "so": new_order, "t": text}).fetchone()
    db.commit()
    return row[0]


def update_sentence_post_id(db: Session, sentence_id: int, post_id: int):
    # Prevent duplicate post_id in the same article
    existing = db.execute(sql_text(
        "SELECT 1 FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = (SELECT sec2.article_id FROM article_sentence s2 "
        "  JOIN article_section sec2 ON s2.section_id = sec2.section_id "
        "  WHERE s2.sentence_id = :sid) "
        "AND s.post_id = :pid AND s.sentence_id != :sid "
        "LIMIT 1"
    ), {"sid": sentence_id, "pid": post_id}).fetchone()
    if existing:
        logger.info("Skipping duplicate post_id=%d link for sentence %d (already in article)", post_id, sentence_id)
        return
    """Link a sentence to its on-chain post_id after registration."""
    db.execute(sql_text(
        "UPDATE article_sentence SET post_id = :p WHERE sentence_id = :s"
    ), {"p": post_id, "s": sentence_id})
    db.commit()


def mark_replaced(db: Session, old_sentence_id: int, new_sentence_id: int):
    """Mark a sentence as replaced by another."""
    db.execute(sql_text(
        "UPDATE article_sentence SET replaced_by = :new WHERE sentence_id = :old"
    ), {"new": new_sentence_id, "old": old_sentence_id})
    db.commit()


def _rebalance_sort_orders(db: Session, section_id: int):
    rows = db.execute(sql_text(
        "SELECT sentence_id FROM article_sentence "
        "WHERE section_id = :s ORDER BY sort_order"
    ), {"s": section_id}).fetchall()
    for i, (sid,) in enumerate(rows):
        db.execute(sql_text(
            "UPDATE article_sentence SET sort_order = :so WHERE sentence_id = :id"
        ), {"so": (i + 1) * 100, "id": sid})
    db.commit()

def refresh_article(db: Session, topic: str) -> bool:
    """Refresh an article by generating new content and merging it with existing.

    Preserves all existing sentences (and their on-chain claim links).
    Only adds new sentences that don't already exist.
    Returns True if new content was added.

    Merge strategy:
      - For each section in the new generation:
        1. If a matching section (by heading) exists, add new sentences to it
        2. If no matching section, create a new section
      - A sentence is "new" if no existing sentence in the article has
        similar text (fuzzy match by normalized lowercase comparison)
      - Existing sentences are never deleted or modified
    """
    from articles.article_gen import generate_article
    from articles.claim_indexer import index_existing_claims_into_article

    key = _norm(topic)
    article = get_article(db, topic)
    if not article:
        return False

    article_id = article["article_id"]

    # Generate fresh content
    try:
        fresh = generate_article(topic)
    except Exception as e:
        logger.warning("Article refresh generation failed for '%s': %s", topic, e)
        return False
    # Validate: reject if the generated title doesn't match the topic
    gen_title = (fresh.get("title") or "").lower().strip()
    topic_lower = topic.lower().strip()
    if gen_title and topic_lower not in gen_title and gen_title not in topic_lower:
        logger.warning("Refresh rejected: generated '%s' but expected '%s'", fresh.get("title"), topic)
        return False

    # Build index of existing sentences for dedup
    existing_texts = set()
    existing_post_ids = set()
    for sec in article["sections"]:
        for sent in sec["sentences"]:
            existing_texts.add(sent["text"].lower().strip())
            if sent.get("post_id") is not None:
                existing_post_ids.add(sent["post_id"])

    # Build index of existing sections (normalized heading -> section_id)
    existing_sections = {}
    for sec in article["sections"]:
        h = sec["heading"].lower().strip()
        existing_sections[h] = sec["section_id"]

    added = 0

    for fresh_sec in fresh.get("sections", []):
        heading = fresh_sec.get("heading", "")
        heading_key = heading.lower().strip()
        new_sents = []

        for sent_text in fresh_sec.get("sentences", []):
            text = str(sent_text).strip()
            if not text:
                continue
            norm = text.lower().strip()
            # Check for near-duplicates (exact match or containment)
            is_dup = False
            for existing in existing_texts:
                if norm == existing or norm in existing or existing in norm:
                    is_dup = True
                    break
            if not is_dup:
                new_sents.append(text)

        if not new_sents:
            continue

        # Find or create the section (fuzzy match to avoid duplicates)
        matched_section = None
        if heading_key in existing_sections:
            matched_section = existing_sections[heading_key]
        else:
            # Try fuzzy heading match before creating a new section
            try:
                from embedding import embed
                from similarity import cosine_similarity
                h_vec = embed(heading_key)
                best_sim = 0.0
                for existing_h, sec_id in existing_sections.items():
                    e_vec = embed(existing_h)
                    sim = cosine_similarity(h_vec, e_vec)
                    if sim > best_sim:
                        best_sim = sim
                        matched_section = sec_id
                if best_sim < 0.75:
                    matched_section = None
            except Exception:
                pass
            if not matched_section:
                # Word overlap fallback
                h_words = set(heading_key.split()) - {"and", "the", "of", "in", "a", "an"}
                for existing_h, sec_id in existing_sections.items():
                    e_words = set(existing_h.split()) - {"and", "the", "of", "in", "a", "an"}
                    overlap = len(h_words & e_words)
                    min_len = min(len(h_words), len(e_words))
                    if min_len > 0 and overlap / min_len >= 0.5 and overlap >= 2:
                        matched_section = sec_id
                        break
        if matched_section:
            section_id = matched_section
        else:
            # Create new section at the end
            max_order = db.execute(sql_text(
                "SELECT COALESCE(MAX(sort_order), 0) FROM article_section WHERE article_id = :a"
            ), {"a": article_id}).fetchone()[0]

            row = db.execute(sql_text(
                "INSERT INTO article_section (article_id, heading, sort_order) "
                "VALUES (:a, :h, :so) RETURNING section_id"
            ), {"a": article_id, "h": heading, "so": max_order + 100}).fetchone()
            section_id = row[0]
            existing_sections[heading_key] = section_id
            logger.info("Created new section '%s' for refresh of '%s'", heading, topic)

        # Get the last sort_order in this section
        last = db.execute(sql_text(
            "SELECT MAX(sort_order) FROM article_sentence WHERE section_id = :s"
        ), {"s": section_id}).fetchone()
        sort_order = (last[0] or 0) + 100

        # Insert new sentences at the end of the section
        for text in new_sents:
            db.execute(sql_text(
                "INSERT INTO article_sentence (section_id, sort_order, text) "
                "VALUES (:s, :so, :t)"
            ), {"s": section_id, "so": sort_order, "t": text})
            existing_texts.add(text.lower().strip())
            sort_order += 100
            added += 1

    # Update timestamps
    db.execute(sql_text(
        "UPDATE topic_article SET last_refreshed_at = NOW(), updated_at = NOW() "
        "WHERE article_id = :a"
    ), {"a": article_id})
    db.commit()

    if added > 0:
        logger.info("Refreshed article '%s': added %d new sentences", topic, added)
        # Re-index existing on-chain claims into the refreshed article
        try:
            index_existing_claims_into_article(db, article_id)
        except Exception as e:
            logger.warning("Post-refresh claim indexing failed: %s", e)

    return added > 0



def build_and_cache_response(db_or_factory, topic_key: str):
    """Build the full enriched article response and cache it as JSONB.
    Called after generation, refresh, or chain event.
    This is the ONLY place that does the expensive work (RPC, embedding, dedup).
    The result is stored so GET /api/article/{topic} can serve it with zero processing."""
    import hashlib, json, logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)

    # Get a session
    if hasattr(db_or_factory, 'execute'):
        db = db_or_factory
        owns_session = False
    else:
        db = db_or_factory()
        owns_session = True

    try:
        article = get_article(db, topic_key)
        if not article:
            return

        article_id = article["article_id"]

        # ── Expensive enrichment (runs once, result is cached) ──

        # 1. SKIPPED: semantic linking is O(sentences * claims), runs only at generation time

        # 2. SKIPPED: index_existing_claims_into_article is slow; runs at generation time instead

        # 3. Enrich with live VS/stake data from chain (RPC calls)
        try:
            from chain.chain_reader import get_stake_totals, get_verity_score
            for section in article.get("sections", []):
                for sent in section.get("sentences", []):
                    pid = sent.get("post_id")
                    if pid is not None:
                        try:
                            s, ch = get_stake_totals(pid)
                            sent["stake_support"] = s
                            sent["stake_challenge"] = ch
                            sent["verity_score"] = get_verity_score(pid)
                        except Exception:
                            sent["stake_support"] = 0
                            sent["stake_challenge"] = 0
                            sent["verity_score"] = 0
                    else:
                        sent["stake_support"] = 0
                        sent["stake_challenge"] = 0
                        sent["verity_score"] = 0
        except ImportError:
            pass

        # 4. Content moderation filter
        try:
            from moderation import check_content_fast
            for section in article.get("sections", []):
                for sent in section.get("sentences", []):
                    mod = check_content_fast(sent.get("text", ""))
                    if not mod.allowed:
                        sent["text"] = "[Content hidden — policy violation]"
                        sent["moderated"] = True
        except Exception:
            pass

        # 5. SKIPPED: _semantic_dedup makes N OpenAI embedding calls per rebuild.
        # Runs at article generation time instead.

        # ── Cache the fully-built result ──
        response_json = json.dumps(article, default=str)
        response_hash = hashlib.md5(response_json.encode()).hexdigest()[:16]

        db.execute(sql_text(
            "UPDATE topic_article SET "
            "cached_response = CAST(:resp AS jsonb), "
            "response_hash = :h, "
            "last_refreshed_at = NOW() "
            "WHERE article_id = :a"
        ), {"resp": response_json, "h": response_hash, "a": article_id})
        db.commit()

        logger.info("Cached article response for '%s' (hash=%s)", topic_key, response_hash)

    except Exception as e:
        logger.warning("build_and_cache_response failed for '%s': %s", topic_key, e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if owns_session:
            db.close()


def apply_stake_delta(
    db_or_factory,
    post_id: int,
    support_total: float,
    challenge_total: float,
    verity_score: float,
):
    """Patch any cached article JSON containing a sentence with this post_id.
    
    Much faster than nulling the cache. Finds sentences in O(articles) and
    updates three fields per match in the JSONB column. Called from the chain
    indexer's StakeAdded / StakeWithdrawn / PostUpdated handler."""
    import json, logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)
    
    if hasattr(db_or_factory, "execute"):
        db = db_or_factory
        owns = False
    else:
        db = db_or_factory()
        owns = True
    try:
        # Find all articles whose cached JSON contains this post_id.
        # The JSON path is sections[*].sentences[*].post_id. We rely on the
        # fact that a matching article_sentence row also exists (indexed link).
        rows = db.execute(sql_text(
            "SELECT DISTINCT ta.article_id, ta.topic_key "
            "FROM topic_article ta "
            "JOIN article_section sec ON sec.article_id = ta.article_id "
            "JOIN article_sentence s ON s.section_id = sec.section_id "
            "WHERE s.post_id = :pid AND ta.cached_response IS NOT NULL"
        ), {"pid": post_id}).fetchall()
        
        for article_id, topic_key in rows:
            try:
                row = db.execute(sql_text(
                    "SELECT cached_response FROM topic_article WHERE article_id = :a"
                ), {"a": article_id}).fetchone()
                if not row or not row[0]:
                    continue
                
                doc = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                patched = False
                for section in doc.get("sections", []):
                    for sent in section.get("sentences", []):
                        if sent.get("post_id") == post_id:
                            sent["stake_support"] = support_total
                            sent["stake_challenge"] = challenge_total
                            sent["verity_score"] = verity_score
                            patched = True
                
                if patched:
                    db.execute(sql_text(
                        "UPDATE topic_article SET cached_response = CAST(:resp AS jsonb), "
                        "last_refreshed_at = NOW() WHERE article_id = :a"
                    ), {"resp": json.dumps(doc, default=str), "a": article_id})
                    logger.info("apply_stake_delta: patched post %d in article '%s'",
                                post_id, topic_key)
            except Exception as e:
                logger.warning("apply_stake_delta failed for article_id=%d: %s",
                               article_id, e)
        db.commit()
    except Exception as e:
        logger.warning("apply_stake_delta failed: %s", e)
        try: db.rollback()
        except Exception: pass
    finally:
        if owns:
            db.close()


def apply_new_post(db_or_factory, post_id: int, claim_text: str):
    """When a new claim is created, link it to any article sentence whose
    text exactly matches (case-insensitive, trimmed). Updates both the
    article_sentence DB row and the cached article JSON.
    
    This is the fast path. Semantic matching (non-exact) runs only during
    full article generation."""
    import json, logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)
    
    if hasattr(db_or_factory, "execute"):
        db = db_or_factory
        owns = False
    else:
        db = db_or_factory()
        owns = True
    try:
        normalized = (claim_text or "").strip()
        if not normalized:
            return
        
        # Find article_sentence rows with matching text that don't yet have post_id
        rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.section_id, sec.article_id, ta.topic_key "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN topic_article ta ON sec.article_id = ta.article_id "
            "WHERE LOWER(TRIM(s.text)) = LOWER(:t) AND s.post_id IS NULL"
        ), {"t": normalized}).fetchall()
        
        if not rows:
            return
        
        # Update each matching sentence
        article_ids = set()
        for sid, _sec_id, art_id, _topic in rows:
            db.execute(sql_text(
                "UPDATE article_sentence SET post_id = :pid WHERE sentence_id = :sid"
            ), {"pid": post_id, "sid": sid})
            article_ids.add(art_id)
        
        # Patch cached JSON for each affected article
        for art_id in article_ids:
            try:
                row = db.execute(sql_text(
                    "SELECT cached_response, topic_key FROM topic_article WHERE article_id = :a"
                ), {"a": art_id}).fetchone()
                if not row or not row[0]:
                    continue
                
                doc = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                patched = False
                for section in doc.get("sections", []):
                    for sent in section.get("sentences", []):
                        if (sent.get("post_id") is None
                            and sent.get("text", "").strip().lower() == normalized.lower()):
                            sent["post_id"] = post_id
                            sent["stake_support"] = 0.0
                            sent["stake_challenge"] = 0.0
                            sent["verity_score"] = 0.0
                            patched = True
                
                if patched:
                    db.execute(sql_text(
                        "UPDATE topic_article SET cached_response = CAST(:resp AS jsonb), "
                        "last_refreshed_at = NOW() WHERE article_id = :a"
                    ), {"resp": json.dumps(doc, default=str), "a": art_id})
                    logger.info("apply_new_post: linked post %d to article_id=%d",
                                post_id, art_id)
            except Exception as e:
                logger.warning("apply_new_post patch failed for article_id=%d: %s",
                               art_id, e)
        
        db.commit()
    except Exception as e:
        logger.warning("apply_new_post failed: %s", e)
        try: db.rollback()
        except Exception: pass
    finally:
        if owns:
            db.close()



def persist_dedup(db, article_id: int):
    """Run dedup using batched embeddings and persist decisions via is_hidden.
    
    Embeddings are cached in article_sentence.embedding (JSONB) so we only embed
    each sentence once across all dedup runs. Drops duplicates by setting is_hidden.
    """
    import json, logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)
    try:
        # Step 1: collect all non-hidden sentences for this article with embeddings from DB
        rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.text, s.post_id, s.embedding "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.is_hidden = FALSE "
            "ORDER BY s.section_id, s.sort_order"
        ), {"a": article_id}).fetchall()
        
        if not rows:
            return
        
        # Step 2: find sentences without cached embeddings and batch-embed them
        needs_embed = [(r[0], r[1]) for r in rows if r[3] is None]
        embeddings_by_id = {}
        for r in rows:
            if r[3] is not None:
                # Already have cached embedding
                if isinstance(r[3], str):
                    embeddings_by_id[r[0]] = json.loads(r[3])
                else:
                    embeddings_by_id[r[0]] = r[3]
        
        if needs_embed:
            from embedding import embed_batch
            texts_to_embed = [t for (_, t) in needs_embed]
            logger.info("persist_dedup: embedding %d new sentences in article %d",
                        len(texts_to_embed), article_id)
            vecs = embed_batch(texts_to_embed)
            
            # Persist new embeddings
            for (sid, _), vec in zip(needs_embed, vecs):
                embeddings_by_id[sid] = vec
                try:
                    db.execute(sql_text(
                        "UPDATE article_sentence SET embedding = CAST(:e AS jsonb) "
                        "WHERE sentence_id = :sid"
                    ), {"e": json.dumps(vec), "sid": sid})
                except Exception as e:
                    logger.debug("Failed to persist embedding for sid=%d: %s", sid, e)
            db.commit()
        
        # Step 3: dedup decision using cached vectors
        from similarity import cosine_similarity
        DEDUP_THRESHOLD = 0.70
        
        onchain_vecs = [embeddings_by_id[r[0]] for r in rows
                        if r[2] is not None and r[0] in embeddings_by_id]
        
        to_hide = []
        kept_offchain_vecs = []
        for r in rows:
            sid, text, post_id, _ = r
            if post_id is not None:
                continue  # always keep on-chain
            text_s = (text or "").strip()
            if not text_s:
                to_hide.append(sid)
                continue
            vec = embeddings_by_id.get(sid)
            if vec is None:
                continue  # couldn't embed, keep
            
            is_dupe = False
            for ov in onchain_vecs:
                if cosine_similarity(vec, ov) >= DEDUP_THRESHOLD:
                    is_dupe = True
                    break
            if not is_dupe:
                for kv in kept_offchain_vecs:
                    if cosine_similarity(vec, kv) >= DEDUP_THRESHOLD:
                        is_dupe = True
                        break
            
            if is_dupe:
                to_hide.append(sid)
            else:
                kept_offchain_vecs.append(vec)
        
        if to_hide:
            db.execute(sql_text(
                "UPDATE article_sentence SET is_hidden = TRUE WHERE sentence_id = ANY(:ids)"
            ), {"ids": to_hide})
            db.commit()
            logger.info("persist_dedup: hid %d duplicates in article %d",
                        len(to_hide), article_id)
    except Exception as e:
        logger.warning("persist_dedup failed for article_id=%d: %s", article_id, e)
        try: db.rollback()
        except Exception: pass


def _get_article_internal(db, article_id: int) -> dict:
    """Internal helper: get article by article_id (not topic_key)."""
    from sqlalchemy import text as sql_text
    row = db.execute(sql_text(
        "SELECT article_id, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not row:
        return None
    topic = row[1]
    return get_article(db, topic)

