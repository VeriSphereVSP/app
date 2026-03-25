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


def ensure_article_for_claim(db: Session, claim_text: str, post_id: int, topic: str):
    """Ensure an article exists for the topic and the claim is in it.
    If the article doesn't exist, generates it in a background thread.
    If it does exist, inserts the claim into the best section."""
    from article_store import get_article, store_article, insert_sentence, update_sentence_post_id, _norm

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
                from claim_indexer import find_best_section
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
            from article_gen import generate_article
            from article_store import store_article, get_article as load_article, insert_sentence, update_sentence_post_id
            from claim_indexer import find_best_section

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
                        from claim_indexer import cross_index_claim_into_all_articles
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
