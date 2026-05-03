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

# Topical relevance threshold for inserting on-chain claims into an
# article. Cosine similarity between the claim's embedding and the
# article's summary embedding (title + topic_key + first sentence of
# each section). Tuned to:
#   - Reject cross-domain content (e.g. a sex-change claim does NOT
#     belong in the Climate Change article even though both texts
#     contain the word "change").
#   - Accept on-domain dissent (e.g. a Milankovitch-cycles claim DOES
#     belong in the Climate Change article even if it argues against
#     the article's main thesis — admitting dissenting on-chain
#     content is the protocol's job).
RELEVANCE_THRESHOLD = 0.5

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


def find_best_section(
    db: Session,
    article_id: int,
    claim_text: str,
    *,
    section_vec_by_id: Optional[dict] = None,
    claim_vec: Optional[list] = None,
) -> Optional[int]:
    """Find the section_id that best matches a claim, or None if no good match.

    Optional precomputed vectors (patch06):
        section_vec_by_id: { section_id: vec } — section embeddings already
            computed by the caller. Skips re-embedding sections.
        claim_vec: vec — the claim's embedding already computed by the
            caller. Skips re-embedding the claim.

    When passed in, find_best_section becomes a pure lookup with one
    cosine_similarity call per section (no network).
    """
    sections = db.execute(sql_text(
        "SELECT sec.section_id, sec.heading "
        "FROM article_section sec WHERE sec.article_id = :a ORDER BY sec.sort_order"
    ), {"a": article_id}).fetchall()

    if not sections:
        return None

    # Try embedding similarity first (accurate for temporal/semantic placement)
    try:
        from embedding import embed
        from similarity import cosine_similarity
        if claim_vec is None:
            claim_vec = embed(claim_text)
        best_id = None
        best_sim = -1.0
        for sec_id, heading in sections:
            sec_vec = (section_vec_by_id or {}).get(sec_id)
            if sec_vec is None:
                sents = db.execute(sql_text(
                    "SELECT text FROM article_sentence WHERE section_id = :s ORDER BY sort_order"
                ), {"s": sec_id}).fetchall()
                sec_text = heading + ". " + " ".join(r[0] for r in sents[:8])
                sec_vec = embed(sec_text)
            sim = cosine_similarity(claim_vec, sec_vec)
            if sim > best_sim:
                best_sim = sim
                best_id = sec_id
        if best_sim >= 0.25:
            return best_id
        logger.debug("No section above embedding threshold (best=%.3f)", best_sim)
    except Exception as e:
        logger.debug("Embedding section match failed, falling back to stems: %s", e)

    # Fallback: stem overlap
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
        if overlap > best_overlap:
            best_overlap = overlap
            best_id = sec_id
    return best_id if best_overlap >= 1 else None


def _article_summary_text(db: Session, article_id: int) -> Optional[str]:
    """Build a representative text summary of an article for embedding.

    Title + topic_key + first sentence of each section. This gives a
    concise but topically-faithful signal: the title and topic_key
    establish the domain, and the first sentence of each section
    captures the article's structure without the noise of long bodies.
    """
    art = db.execute(sql_text(
        "SELECT title, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not art:
        return None
    title, topic_key = art
    sec_rows = db.execute(sql_text(
        "SELECT sec.section_id, sec.heading "
        "FROM article_section sec WHERE sec.article_id = :a "
        "ORDER BY sec.sort_order"
    ), {"a": article_id}).fetchall()
    parts = [str(title or ""), str(topic_key or "")]
    for sec_id, heading in sec_rows:
        if heading:
            parts.append(str(heading))
        first_sent = db.execute(sql_text(
            "SELECT text FROM article_sentence "
            "WHERE section_id = :s AND is_hidden = FALSE "
            "ORDER BY sort_order LIMIT 1"
        ), {"s": sec_id}).fetchone()
        if first_sent and first_sent[0]:
            parts.append(str(first_sent[0]))
    summary = ". ".join(p.strip() for p in parts if p and p.strip())
    return summary or None


def _article_summary_embedding(db: Session, article_id: int):
    """Embed the article summary, or None on failure."""
    summary = _article_summary_text(db, article_id)
    if not summary:
        return None
    try:
        from embedding import embed
        return embed(summary)
    except Exception as e:
        logger.debug("Article summary embedding failed for article %d: %s", article_id, e)
        return None


def _claim_relevance_score(claim_text: str, article_vec) -> Optional[float]:
    """Cosine similarity between claim and article summary, or None on failure."""
    if article_vec is None:
        return None
    try:
        from embedding import embed
        from similarity import cosine_similarity
        cv = embed(claim_text)
        return float(cosine_similarity(cv, article_vec))
    except Exception as e:
        logger.debug("Claim relevance embedding failed: %s", e)
        return None


def is_claim_relevant_to_article(
    db: Session,
    article_id: int,
    claim_text: str,
    article_vec=None,
) -> bool:
    """Check if a claim is topically relevant to an article.

    Strategy:
      1. Strict substring match: if the topic_key or title appears as a
         substring in the claim text, accept (cheap fast-path; high
         precision).
      2. Embedding similarity: cosine(claim, article_summary) ≥
         RELEVANCE_THRESHOLD.
      3. Fallback (embedding failure only): reject. We deliberately do
         NOT fall back to stem overlap, which produced the false
         positive that motivated this fix — "change" matched between
         "Climate Change" and "...can change the sex of a human being"
         and the unrelated claim was injected into the article.

    Args:
        db: SQLAlchemy session.
        article_id: ID in topic_article.
        claim_text: The candidate on-chain claim text.
        article_vec: Optional pre-computed article summary embedding.
            Pass this in when checking many claims against the same
            article to avoid re-embedding the article each time.
    """
    art = db.execute(sql_text(
        "SELECT title, topic_key FROM topic_article WHERE article_id = :a"
    ), {"a": article_id}).fetchone()
    if not art:
        return False

    title, topic_key = art
    claim_lower = (claim_text or "").lower()
    title_lower = (title or "").lower().strip()
    topic_lower = (topic_key or "").lower().strip()

    # Strategy 1: strict substring match on title or topic_key.
    # Only match on non-trivial keys (≥ 4 chars) to avoid false positives
    # from short common words.
    if topic_lower and len(topic_lower) >= 4 and topic_lower in claim_lower:
        return True
    if title_lower and len(title_lower) >= 4 and title_lower in claim_lower:
        return True

    # Strategy 2: embedding similarity.
    if article_vec is None:
        article_vec = _article_summary_embedding(db, article_id)
    sim = _claim_relevance_score(claim_text, article_vec)
    if sim is not None:
        if sim >= RELEVANCE_THRESHOLD:
            logger.debug(
                "Relevance accept: article=%d sim=%.3f claim='%s'",
                article_id, sim, (claim_text or "")[:60],
            )
            return True
        logger.debug(
            "Relevance reject: article=%d sim=%.3f < %.2f claim='%s'",
            article_id, sim, RELEVANCE_THRESHOLD, (claim_text or "")[:60],
        )
        return False

    # Strategy 3: embedding unavailable — be strict, reject.
    # The historical stem-overlap fallback caused the very bug this
    # function exists to prevent. Better to miss a relevant claim than
    # to pollute an article with cross-domain content.
    logger.warning(
        "Relevance check fell through (no embedding available) for "
        "article=%d; rejecting claim conservatively: '%s'",
        article_id, (claim_text or "")[:60],
    )
    return False


def index_existing_claims_into_article(db: Session, article_id: int):
    """Find on-chain claims that belong in an article and overlay them.

    # Collect all post_ids already in this article to prevent duplicates
    existing_pids = set()
    _rows = db.execute(sql_text(
        "SELECT DISTINCT s.post_id FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.post_id IS NOT NULL"
    ), {"a": article_id}).fetchall()
    for (_pid,) in _rows:
        existing_pids.add(_pid)


    Two modes:
    1. OVERLAY: If an article sentence semantically matches an on-chain claim,
       link the sentence to the claim's post_id.
    2. INSERT: If no matching sentence exists but the claim is topically relevant,
       insert it into the best section.
    """
    from articles.article_store import insert_sentence, update_sentence_post_id

    claims = db.execute(sql_text(
        "SELECT claim_id, claim_text, post_id FROM claim "
        "WHERE post_id IS NOT NULL ORDER BY post_id"
    )).fetchall()

    if not claims:
        logger.info("No on-chain claims to index")
        return

    # Compute the article's summary embedding ONCE for reuse across all
    # candidate-claim relevance checks below.
    article_vec = _article_summary_embedding(db, article_id)

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
        if not is_claim_relevant_to_article(db, article_id, ctext, article_vec=article_vec):
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


def cross_index_claim_into_all_articles(db: Session, claim_text: str, post_id: int):
    """Index a single on-chain claim into ALL relevant articles.

    Called by chain/indexer.py whenever a new claim is discovered.
    Checks every existing article for topical relevance and inserts
    the claim into the best-matching section if not already present.
    """
    from articles.article_store import insert_sentence, update_sentence_post_id

    # Get all articles
    articles = db.execute(sql_text(
        "SELECT article_id, topic_key, title FROM topic_article ORDER BY updated_at DESC"
    )).fetchall()

    if not articles:
        return

    claim_lower = claim_text.lower().strip()
    indexed_into = 0

    for article_id, topic_key, title in articles:
        # Check if claim is already in this article (by post_id — authoritative dedup)
        existing = db.execute(sql_text(
            "SELECT 1 FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.post_id = :pid "
            "LIMIT 1"
        ), {"a": article_id, "pid": post_id}).fetchone()

        if existing:
            continue

        # Check topical relevance
        if not is_claim_relevant_to_article(db, article_id, claim_text):
            continue

        # Find best section (uses its own embedding caching path)
        sec_id = find_best_section(db, article_id, claim_text)
        if sec_id is None:
            continue

        # Insert at end of section
        try:
            last_sent = db.execute(sql_text(
                "SELECT sentence_id FROM article_sentence "
                "WHERE section_id = :s ORDER BY sort_order DESC LIMIT 1"
            ), {"s": sec_id}).fetchone()

            after_id = last_sent[0] if last_sent else None
            new_sid = insert_sentence(db, sec_id, after_id, claim_text)
            update_sentence_post_id(db, new_sid, post_id)
            indexed_into += 1
            logger.info(
                "Cross-indexed claim post_id=%d into article '%s' section %d",
                post_id, topic_key, sec_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to cross-index claim post_id=%d into article %d: %s",
                post_id, article_id, e,
            )

    if indexed_into > 0:
        logger.info(
            "Cross-indexed claim post_id=%d ('%s') into %d article(s)",
            post_id, claim_text[:40], indexed_into,
        )



def cleanup_irrelevant_claims_from_article(
    db_or_factory,
    article_id: int,
) -> int:
    """Remove previously-injected on-chain claim sentences that fail
    the current relevance gate.

    Safe to run repeatedly. Lock profile:
      Phase 1 — read all candidate rows + the article-summary embedding
                source (only AccessShareLock).
      Phase 2 — call embed() per candidate to score relevance. No DB
                writes during this phase, so the (slow) network calls
                cannot block another process holding row locks on
                article_sentence or article_section.
      Phase 3 — issue a single batched DELETE for the doomed rows,
                then commit. One fast write transaction.

    Conservative criterion: only removes sentences whose text byte-
    equals the on-chain claim_text (i.e. were INSERTED by the indexer
    rather than merely overlaid onto an existing LLM-generated
    sentence). Overlaid sentences carry a post_id but their text is
    the LLM's wording, which is paraphrased; deleting those would
    take legitimate article content with them.

    Args:
        db_or_factory: an open SQLAlchemy Session OR a session factory
            callable. If a factory is passed, cleanup runs in its own
            fresh session and is fully isolated from any caller
            transaction. Passing a factory is recommended.
        article_id: ID in topic_article.

    Returns the number of sentences removed.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.orm import Session as _Session

    # Use a fresh session if the caller passed a factory. Avoids
    # poisoning the caller's transaction state on any error here, and
    # avoids deadlocking against the caller's own pending writes.
    if isinstance(db_or_factory, _Session):
        db = db_or_factory
        owns_session = False
    else:
        db = db_or_factory()
        owns_session = True

    try:
        # ── Phase 1: read everything we might need (read-only) ──
        article_vec = _article_summary_embedding(db, article_id)
        if article_vec is None:
            logger.debug(
                "Cleanup: article %d summary embedding unavailable; skipping",
                article_id,
            )
            return 0

        rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.post_id, c.claim_text "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN claim c ON c.post_id = s.post_id "
            "WHERE sec.article_id = :a "
            "  AND s.post_id IS NOT NULL "
            "  AND LOWER(TRIM(s.text)) = LOWER(TRIM(c.claim_text))"
        ), {"a": article_id}).fetchall()
        if not rows:
            return 0

        # ── Phase 2: decide what to delete (no writes, embed() calls) ──
        doomed = []  # list of (sentence_id, post_id, claim_text)
        for sentence_id, post_id, claim_text in rows:
            if is_claim_relevant_to_article(
                db, article_id, claim_text, article_vec=article_vec,
            ):
                continue
            doomed.append((int(sentence_id), int(post_id), str(claim_text)))

        if not doomed:
            return 0

        # ── Phase 3: single batched DELETE + commit ──
        try:
            db.execute(sql_text(
                "DELETE FROM article_sentence "
                "WHERE sentence_id = ANY(:ids)"
            ), {"ids": [d[0] for d in doomed]})
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "Cleanup: batched DELETE failed for article %d: %s",
                article_id, e,
            )
            return 0

        for sid, pid, ctext in doomed:
            logger.info(
                "Cleanup: removed irrelevant injected sentence %d "
                "(post_id=%d) from article %d: '%s'",
                sid, pid, article_id, (ctext or "")[:60],
            )
        return len(doomed)
    finally:
        if owns_session:
            try:
                db.close()
            except Exception:
                pass
