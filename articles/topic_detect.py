# app/topic_detect.py
"""
Auto-detect a topic from claim text using the LLM.
Used when claims are created outside of an article context.
"""
import logging
import threading
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from llm_provider import complete

logger = logging.getLogger(__name__)

TOPIC_SYSTEM = """You are a topic classifier. Given a factual claim, return the most appropriate
encyclopedia topic title that this claim belongs to. The topic should be:
- A short noun phrase (1-4 words), like "Climate Change", "Bitcoin", "Earth"
- Broad enough to be an encyclopedia article title
- Specific enough to be meaningful

Return ONLY the topic title, nothing else. No quotes, no explanation."""


def detect_topic(claim_text: str) -> Optional[str]:
    """Detect the best topic for a claim. Returns a short topic string."""
    try:
        result = complete(
            prompt=f"Claim: {claim_text}",
            system=TOPIC_SYSTEM,
            max_tokens=50,
            temperature=0.1,
        )
        topic = result.strip().strip('"').strip("'").strip()
        # Clean up: remove trailing version numbers like "COVID-19" -> "COVID-19" (keep)
        # but avoid returning just numbers
        topic = topic.rstrip('.')
        # Sanity check — should be short
        if topic and len(topic) < 100:
            return topic
        return None
    except Exception as e:
        logger.warning("Topic detection failed: %s", e)
        return None


def snap_topic(db: Session, post_id: int, threshold: float = None):
    """Cluster-then-label: return the topic of the nearest ALREADY-topiced claim
    if the new claim (post_id) is >= threshold cosine-similar to it, else None.

    The grouping engine is scoped per-topic, so near-identical claims MUST share a
    topic to be compared. Rather than classify each claim independently (which let
    'Climate' and 'Climate Change' split two 0.98-similar claims), a new claim
    inherits its nearest neighbour's topic. The new claim's embedding is already in
    chain_claim_text by the time this runs (embed_claim precedes topic detection in
    the derived-state worker). Read-only. patch_topic_snap.

    Threshold via VSP_TOPIC_SNAP_THRESHOLD (default 0.95, matching the group bar).
    """
    import os as _os
    if threshold is None:
        threshold = float(_os.getenv("VSP_TOPIC_SNAP_THRESHOLD", "0.95"))
    try:
        row = db.execute(sql_text(
            "SELECT c.topic, (ctn.embedding <=> cto.embedding) AS dist "
            "FROM chain_claim_text ctn "
            "JOIN chain_claim_text cto ON cto.post_id <> ctn.post_id "
            "JOIN claim c ON c.claim_text = cto.claim_text "
            "WHERE ctn.post_id = :pid "
            "  AND ctn.embedding IS NOT NULL AND cto.embedding IS NOT NULL "
            "  AND c.topic IS NOT NULL AND btrim(c.topic) <> '' "
            "ORDER BY dist ASC LIMIT 1"
        ), {"pid": post_id}).fetchone()
    except Exception as e:
        logger.warning("snap_topic query failed for post %d: %s", post_id, e)
        return None
    if row and row[1] is not None:
        sim = 1.0 - float(row[1])
        if sim >= threshold:
            logger.info("topic snap: post %d inherits topic %r (cosine %.4f >= %.2f)",
                        post_id, row[0], sim, threshold)
            return row[0]
    return None


def ensure_article_for_claim(db: Session, claim_text: str, post_id: int, topic: str):
    """Ensure an article exists for the topic and the claim is in it.
    If the article doesn't exist, generates it in a background thread.
    If it does exist, inserts the claim into the best section."""
    from articles.article_store import get_article, store_article, insert_sentence, update_sentence_post_id, _norm

    topic_key = _norm(topic)

    # Check if article already exists (exact match or similar key)
    existing = db.execute(sql_text(
        "SELECT article_id, topic_key FROM topic_article "
        "WHERE topic_key = :k OR topic_key LIKE :prefix OR :k LIKE topic_key || '%'"
        " LIMIT 1"
    ), {"k": topic_key, "prefix": topic_key + "%"}).fetchone()

    if existing:
        article_id = existing[0]
        # Check if claim is already in this article
        already = db.execute(sql_text(
            "SELECT 1 FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.post_id = :pid LIMIT 1"
        ), {"a": article_id, "pid": post_id}).fetchone()

        if not already:
            # Insert into best section
            try:
                from articles.claim_indexer import find_best_section
                sec_id = find_best_section(db, article_id, claim_text)
                if sec_id:
                    last = db.execute(sql_text(
                        "SELECT sentence_id FROM article_sentence "
                        "WHERE section_id = :s ORDER BY sort_order DESC LIMIT 1"
                    ), {"s": sec_id}).fetchone()
                    after_id = last[0] if last else None
                    new_sid = insert_sentence(db, sec_id, after_id, claim_text)
                    update_sentence_post_id(db, new_sid, post_id)
                    logger.info("Inserted claim post_id=%d into existing article '%s'", post_id, topic)
            except Exception as e:
                logger.warning("Failed to insert claim into existing article: %s", e)
        return

    # Article doesn't exist — generate in background
    def _generate():
        try:
            from db import get_session_factory
            from articles.article_gen import generate_article
            from articles.article_store import store_article, get_article as load_article, insert_sentence, update_sentence_post_id
            from articles.claim_indexer import find_best_section

            session = get_session_factory()()
            try:
                logger.info("Background article generation starting for '%s'", topic)
                result = generate_article(topic)
                store_article(session, topic, result["title"], result["sections"])

                # Now insert the claim
                article = load_article(session, topic)
                if article:
                    sec_id = find_best_section(session, article["article_id"], claim_text)
                    if sec_id:
                        last = session.execute(sql_text(
                            "SELECT sentence_id FROM article_sentence "
                            "WHERE section_id = :s ORDER BY sort_order DESC LIMIT 1"
                        ), {"s": sec_id}).fetchone()
                        after_id = last[0] if last else None
                        new_sid = insert_sentence(session, sec_id, after_id, claim_text)
                        update_sentence_post_id(session, new_sid, post_id)

                    # Also cross-index into other relevant articles
                    try:
                        from articles.claim_indexer import cross_index_claim_into_all_articles
                        cross_index_claim_into_all_articles(session, claim_text, post_id)
                    except Exception:
                        pass

                logger.info("Background article generation complete for '%s'", topic)
            finally:
                session.close()
        except Exception as e:
            logger.error("Background article generation failed for '%s': %s", topic, e)

    thread = threading.Thread(target=_generate, daemon=True, name=f"article-gen-{topic[:20]}")
    thread.start()
    logger.info("Started background article generation for '%s'", topic)
