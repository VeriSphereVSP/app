# app/claim_indexer.py
"""
Cross-topic claim indexer.

When a new article is generated, find existing on-chain claims that
belong in it and insert them into the best-matching section.

Uses simple stem-word overlap for matching — no embedding dependency.
"""
import re
import logging
from typing import Set, Dict, Any, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

from semantic import find_best_onchain_match, find_all_onchain_matches, OVERLAY_THRESHOLD

# Simple stop words
_STOP = {"a","an","the","is","are","was","were","be","been","being","have","has",
         "had","do","does","did","will","would","shall","should","may","might",
         "can","could","of","in","to","for","with","on","at","by","from","as",
         "into","through","during","about","and","but","or","not","no","its",
         "it","this","that","these","those","than","very","just","also"}


def _stems(text: str) -> Set[str]:
    """Extract lowercased word stems (crude: strip common suffixes)."""
    words = re.findall(r'[a-zA-Z]{2,}', text.lower())
    result = set()
    for w in words:
        if w in _STOP:
            continue
        # Crude stemming: strip common suffixes
        for suffix in ("ing", "tion", "sion", "ness", "ment", "ity", "ous",
                       "ive", "able", "ible", "ally", "ful", "less", "ly",
                       "ed", "er", "est", "es", "al", "en"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[:-len(suffix)]
                break
        result.add(w)
    return result


def _section_stems(section_sentences: List[str], heading: str) -> Set[str]:
    """Get all stems from a section's heading + sentences."""
    all_text = heading + " " + " ".join(section_sentences)
    return _stems(all_text)


def find_best_section(db: Session, article_id: int, claim_text: str) -> Optional[int]:
    """Find the section_id that best matches a claim, or None if no good match."""
    sections = db.execute(sql_text(
        "SELECT sec.section_id, sec.heading "
        "FROM article_section sec WHERE sec.article_id = :a ORDER BY sec.sort_order"
    ), {"a": article_id}).fetchall()

    if not sections:
        return None

    claim_st = _stems(claim_text)
    if not claim_st:
        return None

    best_id = None
    best_overlap = 0

    for sec_id, heading in sections:
        sents = db.execute(sql_text(
            "SELECT text FROM article_sentence WHERE section_id = :s ORDER BY sort_order"
        ), {"s": sec_id}).fetchall()
        sec_st = _section_stems([r[0] for r in sents], heading)
        overlap = len(claim_st & sec_st)
        # Require at least 1 overlapping stem beyond just the topic name
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = sec_id

    # Threshold: need at least 1 stem overlap
    return best_id if best_overlap >= 1 else None


def is_claim_relevant_to_article(db: Session, article_id: int, claim_text: str) -> bool:
    """Check if a claim is relevant to an article by comparing with article title and content."""
    art = db.execute(sql_text(
        "SELECT title, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not art:
        return False

    title, topic_key = art
    claim_lower = claim_text.lower()
    title_lower = title.lower()
    topic_lower = topic_key.lower()

    # Quick check: does the claim mention the topic?
    if topic_lower in claim_lower or title_lower in claim_lower:
        return True

    # Check stem overlap with topic
    claim_st = _stems(claim_text)
    topic_st = _stems(title)
    if claim_st & topic_st:
        return True

    return False


def index_existing_claims_into_article(db: Session, article_id: int):
    """Find on-chain claims that belong in an article and overlay them.

    Two modes:
    1. OVERLAY: If an article sentence semantically matches an on-chain claim,
       link the sentence to the claim's post_id.
    2. INSERT: If no matching sentence exists but the claim is topically relevant,
       insert it into the best section.
    """
    from article_store import insert_sentence, update_sentence_post_id

    claims = db.execute(sql_text(
        "SELECT claim_id, claim_text, post_id FROM claim "
        "WHERE post_id IS NOT NULL ORDER BY post_id"
    )).fetchall()

    if not claims:
        logger.info("No on-chain claims to index")
        return

    sentences = db.execute(sql_text(
        "SELECT s.sentence_id, s.text, s.post_id, sec.section_id "
        "FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a "
        "ORDER BY sec.sort_order, s.sort_order"
    ), {"a": article_id}).fetchall()

    existing_texts = {r[1].lower().strip() for r in sentences}
    existing_pids = {r[2] for r in sentences if r[2] is not None}

    indexed_count = 0

    for cid, ctext, pid in claims:
        ctext_key = ctext.lower().strip()

        if pid in existing_pids:
            continue

        # Strategy 1: OVERLAY — find a sentence that semantically matches this claim
        overlaid = False
        for sent_id, sent_text, sent_pid, sec_id in sentences:
            if sent_pid is not None:
                continue
            try:
                matches = find_all_onchain_matches(db, sent_text, top_k=3)
                for m in matches:
                    if m["post_id"] == pid and m["similarity"] >= OVERLAY_THRESHOLD:
                        update_sentence_post_id(db, sent_id, pid)
                        existing_pids.add(pid)
                        indexed_count += 1
                        overlaid = True
                        logger.info(
                            "Overlaid claim post_id=%d onto sentence %d (sim=%.3f): '%s' <-> '%s'",
                            pid, sent_id, m["similarity"], ctext[:40], sent_text[:40],
                        )
                        break
            except Exception as e:
                logger.debug("Semantic overlay check failed for sent %d: %s", sent_id, e)
            if overlaid:
                break

        if overlaid:
            continue

        # Strategy 2: Exact text match already in article but not linked
        if ctext_key in existing_texts:
            try:
                unlinked = db.execute(sql_text(
                    "SELECT s.sentence_id FROM article_sentence s "
                    "JOIN article_section sec ON s.section_id = sec.section_id "
                    "WHERE sec.article_id = :a AND LOWER(TRIM(s.text)) = :t "
                    "AND s.post_id IS NULL LIMIT 1"
                ), {"a": article_id, "t": ctext_key}).fetchone()
                if unlinked:
                    update_sentence_post_id(db, unlinked[0], pid)
                    existing_pids.add(pid)
                    indexed_count += 1
                    logger.info("Linked existing sentence %d to claim post_id=%d '%s'",
                                unlinked[0], pid, ctext[:40])
            except Exception as e:
                logger.warning("Failed to link existing sentence to post_id=%d: %s", pid, e)
            continue

        # Strategy 3: INSERT — claim is relevant but no matching sentence exists
        if not is_claim_relevant_to_article(db, article_id, ctext):
            continue

        sec_id = find_best_section(db, article_id, ctext)
        if sec_id is None:
            continue

        last_sent = db.execute(sql_text(
            "SELECT sentence_id FROM article_sentence "
            "WHERE section_id = :s ORDER BY sort_order DESC LIMIT 1"
        ), {"s": sec_id}).fetchone()

        after_id = last_sent[0] if last_sent else None
        try:
            new_sid = insert_sentence(db, sec_id, after_id, ctext)
            update_sentence_post_id(db, new_sid, pid)
            existing_texts.add(ctext_key)
            existing_pids.add(pid)
            indexed_count += 1
            logger.info("Inserted on-chain claim post_id=%d '%s' into article %d section %d",
                        pid, ctext[:40], article_id, sec_id)
        except Exception as e:
            logger.warning("Failed to index claim post_id=%d: %s", pid, e)

    logger.info("Indexed %d on-chain claims into article %d", indexed_count, article_id)
