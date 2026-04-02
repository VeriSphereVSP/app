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
            "FROM article_sentence WHERE section_id = :s ORDER BY sort_order"
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

        # 1. Link unlinked sentences to on-chain claims via embedding similarity
        try:
            from articles.article_routes import _link_unlinked_sentences
            _link_unlinked_sentences(db, article)
        except Exception as e:
            logger.debug("Link unlinked failed: %s", e)

        # 2. Index existing on-chain claims into this article
        try:
            from articles.claim_indexer import index_existing_claims_into_article
            index_existing_claims_into_article(db, article_id)
            article = get_article(db, topic_key)
            if not article:
                return
        except Exception as e:
            logger.debug("Claim indexing failed: %s", e)

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

        # 5. Semantic dedup
        try:
            from articles.article_routes import _semantic_dedup
            _semantic_dedup(article)
        except Exception:
            pass

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
