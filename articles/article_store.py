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
    """No-op. Schema is managed by ops/compose/migrations/*.sql, applied
    at container startup via `python migrate.py`. This stub remains for
    backward compatibility with callers that expect the function to exist."""
    return

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

    sections = [s for s in sections if s.get("sentences")]
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




def invalidate_article_cache(db: Session, sentence_id: int):
    """Invalidate the cached article response for the article containing this sentence.
    Also sets last_refreshed_at = NULL so the background refresh picks it up next."""
    try:
        row = db.execute(sql_text(
            "SELECT ta.article_id, ta.topic_key FROM topic_article ta "
            "JOIN article_section sec ON sec.article_id = ta.article_id "
            "JOIN article_sentence s ON s.section_id = sec.section_id "
            "WHERE s.sentence_id = :sid LIMIT 1"
        ), {"sid": sentence_id}).fetchone()
        if row:
            article_id, topic_key = row
            db.execute(sql_text(
                "UPDATE topic_article SET "
                "last_refreshed_at = NULL WHERE article_id = :a"
            ), {"a": article_id})
            db.commit()
            logger.info("Cache invalidated for article %d (%s) — queued for priority refresh",
                        article_id, topic_key)
    except Exception as e:
        logger.warning("Cache invalidation failed for sentence %d: %s", sentence_id, e)
        try:
            db.rollback()
        except Exception:
            pass

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

    # Pre-compute embeddings of existing sentences for semantic dedup during refresh
    _refresh_embeddings = []
    try:
        from embedding import embed_batch
        _existing_texts_list = [t for t in existing_texts if t.strip()]
        if _existing_texts_list:
            _refresh_embeddings = embed_batch(list(_existing_texts_list))
    except Exception as _emb_err:
        import logging as _lg
        _lg.getLogger(__name__).debug("Refresh embedding precompute failed: %s", _emb_err)

    # Build index of existing sections (normalized heading -> section_id)
    existing_sections = {}
    for sec in article["sections"]:
        h = sec["heading"].lower().strip()
        existing_sections[h] = sec["section_id"]

    # ── Batch-precompute existing-heading embeddings ONCE (patch06).
    # The fuzzy-heading match below used to call embed(existing_h)
    # inside a nested loop, re-embedding the same headings on every
    # fresh section. Compute them once upfront.
    _existing_heading_vecs = {}  # normalized heading -> vec
    try:
        from embedding import embed_batch as _eb
        _heading_keys = list(existing_sections.keys())
        if _heading_keys:
            _heading_vec_list = _eb(_heading_keys)
            _existing_heading_vecs = dict(zip(_heading_keys, _heading_vec_list))
    except Exception as _he:
        import logging as _lg
        _lg.getLogger(__name__).debug("Existing-heading embed precompute failed: %s", _he)

    # ── Batch-precompute fresh-sentence embeddings ONCE (patch06).
    # Was: per-fresh-sentence embed() inside the nested loop. Now one
    # batched call. Indexed by (fresh_sec_idx, fresh_sent_idx).
    _fresh_sentence_vecs = {}  # (fs_idx, ss_idx) -> vec
    try:
        from embedding import embed_batch as _eb2
        _fresh_keys = []
        _fresh_texts = []
        for _fs_idx, _fs in enumerate(fresh.get("sections", [])):
            for _ss_idx, _stext in enumerate(_fs.get("sentences", [])):
                _t = str(_stext).strip()
                if _t:
                    _fresh_keys.append((_fs_idx, _ss_idx))
                    _fresh_texts.append(_t)
        if _fresh_texts:
            _fresh_vec_list = _eb2(_fresh_texts)
            _fresh_sentence_vecs = dict(zip(_fresh_keys, _fresh_vec_list))
    except Exception as _fe:
        import logging as _lg
        _lg.getLogger(__name__).debug("Fresh-sentence embed precompute failed: %s", _fe)

    # ── Batch-precompute fresh-heading embeddings ONCE (patch06) ──
    _fresh_heading_vecs = {}  # heading_key -> vec
    try:
        from embedding import embed_batch as _eb3
        _fhk = []
        _fht = []
        for _fs in fresh.get("sections", []):
            _hk = (_fs.get("heading") or "").lower().strip()
            if _hk and _hk not in _fresh_heading_vecs and _hk not in _fhk:
                _fhk.append(_hk)
                _fht.append(_hk)
        if _fht:
            _fhv = _eb3(_fht)
            _fresh_heading_vecs = dict(zip(_fhk, _fhv))
    except Exception as _the:
        import logging as _lg
        _lg.getLogger(__name__).debug("Fresh-heading embed precompute failed: %s", _the)

    added = 0

    for _fs_idx, fresh_sec in enumerate(fresh.get("sections", [])):
        heading = fresh_sec.get("heading", "")
        heading_key = heading.lower().strip()
        new_sents = []

        for _ss_idx, sent_text in enumerate(fresh_sec.get("sentences", [])):
            text = str(sent_text).strip()
            if not text:
                continue
            norm = text.lower().strip()
            # Check for near-duplicates: exact match, containment, or semantic similarity
            is_dup = False
            for existing in existing_texts:
                if norm == existing or norm in existing or existing in norm:
                    is_dup = True
                    break
            # Semantic check using precomputed vectors (patch06).
            if not is_dup and _refresh_embeddings:
                try:
                    from similarity import cosine_similarity
                    new_vec = _fresh_sentence_vecs.get((_fs_idx, _ss_idx))
                    if new_vec is None:
                        # Fallback if precompute missed this entry.
                        from embedding import embed
                        new_vec = embed(text)
                    for evec in _refresh_embeddings:
                        if cosine_similarity(new_vec, evec) >= 0.80:
                            is_dup = True
                            break
                except Exception:
                    pass
            if not is_dup:
                new_sents.append(text)

        if not new_sents:
            continue

        # Find or create the section (fuzzy match to avoid duplicates)
        matched_section = None
        if heading_key in existing_sections:
            matched_section = existing_sections[heading_key]
        else:
            # Try fuzzy heading match using precomputed vectors (patch06).
            try:
                from similarity import cosine_similarity
                h_vec = _fresh_heading_vecs.get(heading_key)
                if h_vec is None:
                    from embedding import embed
                    h_vec = embed(heading_key)
                best_sim = 0.0
                for existing_h, sec_id in existing_sections.items():
                    e_vec = _existing_heading_vecs.get(existing_h)
                    if e_vec is None:
                        from embedding import embed
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



def _reassign_sentences_to_best_sections(db, article_id: int):
    """Move sentences to the section whose heading best matches their content.
    Only moves if the match is significantly better than the current section."""
    import logging
    from sqlalchemy import text as sql_text
    _logger = logging.getLogger(__name__)

    sections = db.execute(sql_text(
        "SELECT section_id, heading FROM article_section WHERE article_id = :a ORDER BY sort_order"
    ), {"a": article_id}).fetchall()
    if len(sections) < 2:
        return

    # Get all non-hidden sentences with their current section
    sents = db.execute(sql_text(
        "SELECT s.sentence_id, s.section_id, s.text "
        "FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "WHERE sec.article_id = :a AND s.is_hidden = FALSE"
    ), {"a": article_id}).fetchall()

    if not sents:
        return

    try:
        from embedding import embed_batch, embed
        from similarity import cosine_similarity

        # ── Batch-embed all section headings in one call ──
        heading_map = {sec[0]: sec[1] for sec in sections}
        heading_ids = [sec[0] for sec in sections]
        heading_texts = [sec[1] for sec in sections]
        try:
            heading_vec_list = embed_batch(heading_texts)
            heading_vecs = {sid: v for sid, v in zip(heading_ids, heading_vec_list)}
        except Exception as he:
            _logger.warning(
                "_reassign_sentences_to_best_sections: heading embed_batch failed (%s); "
                "falling back to per-heading embed()", he,
            )
            heading_vecs = {sid: embed(h) for sid, h in zip(heading_ids, heading_texts)}

        # ── Batch-embed all candidate sentences using the
        #    article_sentence.embedding pgvector cache ──
        # Helpers (duplicated for module-local scope; same as in
        # build_and_cache_response and persist_dedup).
        def _parse_pgvec(s):
            if s is None:
                return None
            s = s.strip()
            if not s.startswith("[") or not s.endswith("]"):
                return None
            try:
                return [float(x) for x in s[1:-1].split(",")]
            except (ValueError, TypeError):
                return None

        def _fmt_pgvec(v):
            return "[" + ",".join(repr(float(x)) for x in v) + "]"

        # Re-fetch including cached embeddings.
        sent_rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.section_id, s.text, s.embedding::text "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.is_hidden = FALSE"
        ), {"a": article_id}).fetchall()

        sent_vec_by_id = {}
        missing = []
        for sid, _csec, txt, emb_text in sent_rows:
            if not txt or not str(txt).strip():
                continue
            parsed = _parse_pgvec(emb_text)
            if parsed is not None:
                sent_vec_by_id[int(sid)] = parsed
            else:
                missing.append((int(sid), str(txt)))

        if missing:
            try:
                new_vecs = embed_batch([t for _, t in missing])
                for (sid, _t), vec in zip(missing, new_vecs):
                    sent_vec_by_id[sid] = vec
                    try:
                        db.execute(sql_text(
                            "UPDATE article_sentence "
                            "SET embedding = (:v)::vector "
                            "WHERE sentence_id = :sid"
                        ), {"v": _fmt_pgvec(vec), "sid": sid})
                    except Exception as ue:
                        _logger.warning(
                            "_reassign_sentences_to_best_sections: failed to persist sentence embedding sid=%d: %s",
                            sid, ue,
                        )
                        db.rollback()
                try:
                    db.commit()
                except Exception:
                    db.rollback()
            except Exception as be:
                _logger.warning(
                    "_reassign_sentences_to_best_sections: sentence embed_batch failed (%s); "
                    "falling back to per-sentence embed()", be,
                )
                for sid, t in missing:
                    try:
                        sent_vec_by_id[sid] = embed(t)
                    except Exception:
                        pass

        # ── Compute reassignments ──
        moved = 0
        moves = []  # list of (sentence_id, new_section_id)
        for sent_id, current_sec, text in sents:
            if not text or not text.strip():
                continue
            sent_vec = sent_vec_by_id.get(int(sent_id))
            if sent_vec is None:
                continue
            best_sec = current_sec
            best_sim = 0.0
            current_sim = 0.0
            for sec_id, hvec in heading_vecs.items():
                sim = cosine_similarity(sent_vec, hvec)
                if sec_id == current_sec:
                    current_sim = sim
                if sim > best_sim:
                    best_sim = sim
                    best_sec = sec_id
            if best_sec != current_sec and best_sim - current_sim > 0.05:
                moves.append((sent_id, best_sec))
                moved += 1

        # ── Apply moves in batches grouped by destination section ──
        if moves:
            from collections import defaultdict
            by_dest = defaultdict(list)
            for sid, new_sec in moves:
                by_dest[new_sec].append(sid)
            for new_sec, sids in by_dest.items():
                try:
                    db.execute(sql_text(
                        "UPDATE article_sentence "
                        "SET section_id = :new_sec "
                        "WHERE sentence_id = ANY(:ids)"
                    ), {"new_sec": new_sec, "ids": sids})
                except Exception as ue:
                    _logger.warning(
                        "_reassign_sentences_to_best_sections: batched move to section %d failed: %s",
                        new_sec, ue,
                    )
                    db.rollback()
            try:
                db.commit()
                _logger.info("Reassigned %d sentences to better sections in article %d", moved, article_id)
            except Exception:
                db.rollback()
    except Exception as e:
        _logger.debug("Section reassignment embedding failed: %s", e)


def build_and_cache_response(db_or_factory, topic_key: str):
    """Build the article response: overlay on-chain masters, dedup, enrich, cache.

    Clean pipeline:
      1. Load article skeleton (sections + all non-hidden sentences)
      2. Find all on-chain claims for this topic, rolled up into dupe-group masters
      3. For each master: hide similar off-chain sentences, ensure master is placed
      4. Prune empty sections, enrich with VS/stake from DB
      5. Cache as JSONB
    """
    import hashlib, json, logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)

    if hasattr(db_or_factory, 'execute'):
        db = db_or_factory
        owns = False
    else:
        db = db_or_factory()
        owns = True

    try:
        key = _norm(topic_key)
        art_row = db.execute(sql_text(
            "SELECT article_id, title FROM topic_article WHERE topic_key = :k"
        ), {"k": key}).fetchone()
        if not art_row:
            return
        article_id, title = art_row

        # ── Step 1: Load article skeleton ──
        secs = db.execute(sql_text(
            "SELECT section_id, heading, sort_order "
            "FROM article_section WHERE article_id = :a ORDER BY sort_order"
        ), {"a": article_id}).fetchall()

        sections = {}  # section_id -> {heading, sentences: [...]}
        for sec_id, heading, _ in secs:
            sents = db.execute(sql_text(
                "SELECT sentence_id, sort_order, text, post_id "
                "FROM article_sentence "
                "WHERE section_id = :s AND is_hidden = FALSE "
                "ORDER BY sort_order"
            ), {"s": sec_id}).fetchall()
            sections[sec_id] = {
                "section_id": sec_id,
                "heading": heading,
                "sentences": [{
                    "sentence_id": r[0], "sort_order": r[1],
                    "text": r[2], "post_id": r[3],
                } for r in sents],
            }

        # ── Step 2: Find on-chain claims for this topic ──
        # Claims associated via article_sentence.post_id OR via topic detection
        onchain_pids = set()
        for sec in sections.values():
            for s in sec["sentences"]:
                if s["post_id"] is not None:
                    onchain_pids.add(s["post_id"])

        # Also find claims tagged with this topic but not yet in the article
        topic_claims = db.execute(sql_text(
            "SELECT ct.post_id, ct.claim_text, ct.dupe_group_id "
            "FROM chain_claim_text ct "
            "JOIN claim c ON c.post_id = ct.post_id "
            "WHERE LOWER(c.topic) = :t AND ct.post_id IS NOT NULL"
        ), {"t": key}).fetchall()
        for r in topic_claims:
            onchain_pids.add(r[0])

        # Build master claims from dupe groups
        # master_claims: [{post_id, text, group_id, member_pids, support, challenge, vs}]
        master_claims = []
        seen_groups = set()
        seen_pids = set()

        for pid in onchain_pids:
            if pid in seen_pids:
                continue
            # Check dupe group
            dg = db.execute(sql_text(
                "SELECT ct.dupe_group_id, g.canonical_post_id, g.canonical_text, "
                "       g.total_support, g.total_challenge, g.aggregate_vs, g.member_count "
                "FROM chain_claim_text ct "
                "LEFT JOIN claim_dupe_group g ON ct.dupe_group_id = g.group_id "
                "WHERE ct.post_id = :pid"
            ), {"pid": pid}).fetchone()

            if dg and dg[0] and dg[0] not in seen_groups and dg[6] and dg[6] > 1:
                # Multi-member group: use canonical as master
                group_id = dg[0]
                seen_groups.add(group_id)
                # Get all member pids
                members = db.execute(sql_text(
                    "SELECT post_id FROM chain_claim_text WHERE dupe_group_id = :gid"
                ), {"gid": group_id}).fetchall()
                member_pids = [m[0] for m in members]
                for mp in member_pids:
                    seen_pids.add(mp)
                master_claims.append({
                    "post_id": dg[1],  # canonical
                    "text": dg[2],
                    "group_id": group_id,
                    "member_pids": member_pids,
                    "member_count": dg[6],
                    "support": dg[3] or 0,
                    "challenge": dg[4] or 0,
                    "vs": dg[5] or 0,
                })
            else:
                # Singleton or no group
                seen_pids.add(pid)
                info = db.execute(sql_text(
                    "SELECT ct.claim_text, COALESCE(p.support_total,0), "
                    "       COALESCE(p.challenge_total,0), COALESCE(p.effective_vs,0) "
                    "FROM chain_claim_text ct "
                    "LEFT JOIN chain_post p ON ct.post_id = p.post_id "
                    "WHERE ct.post_id = :pid"
                ), {"pid": pid}).fetchone()
                if info:
                    master_claims.append({
                        "post_id": pid,
                        "text": info[0],
                        "group_id": dg[0] if dg else None,
                        "member_pids": [pid],
                        "member_count": 1,
                        "support": info[1],
                        "challenge": info[2],
                        "vs": info[3],
                    })

        # ── Step 3: For each master, hide similar off-chain, ensure placed ──
        #
        # Performance: master_vec and sent_vec are computed ONCE in batched
        # embed_batch() calls and reused across the master loop. Off-chain
        # sentence embeddings are cached in article_sentence.embedding so
        # subsequent rebuilds usually need zero embedding network calls.
        # Mirrors the persist_dedup() caching pattern. See patch06.
        try:
            from embedding import embed, embed_batch
            from similarity import cosine_similarity
        except ImportError:
            embed = None
            embed_batch = None
            cosine_similarity = None

        SIMILARITY_THRESHOLD = 0.80

        # Helpers shared with persist_dedup() — pgvector text-format I/O.
        def _parse_pgvec(s):
            if s is None:
                return None
            s = s.strip()
            if not s.startswith("[") or not s.endswith("]"):
                return None
            try:
                return [float(x) for x in s[1:-1].split(",")]
            except (ValueError, TypeError):
                return None

        def _fmt_pgvec(v):
            return "[" + ",".join(repr(float(x)) for x in v) + "]"

        # Build sentence_vec_by_id: { sentence_id: list[float] } for every
        # off-chain sentence in this article. Reads cached embeddings;
        # batches a single embed_batch() call for any that are missing.
        sentence_vec_by_id = {}
        if embed_batch and cosine_similarity:
            try:
                rows = db.execute(sql_text(
                    "SELECT s.sentence_id, s.text, s.embedding::text "
                    "FROM article_sentence s "
                    "JOIN article_section sec ON s.section_id = sec.section_id "
                    "WHERE sec.article_id = :a "
                    "  AND s.post_id IS NULL "
                    "  AND s.is_hidden = FALSE"
                ), {"a": article_id}).fetchall()

                cached = []
                missing = []
                for sid, txt, emb_text in rows:
                    parsed = _parse_pgvec(emb_text)
                    if parsed is not None:
                        sentence_vec_by_id[int(sid)] = parsed
                        cached.append(int(sid))
                    else:
                        missing.append((int(sid), str(txt)))

                if missing:
                    logger.info(
                        "build_and_cache_response: embedding %d new off-chain sentence(s) for article %d (%d cached)",
                        len(missing), article_id, len(cached),
                    )
                    try:
                        new_vecs = embed_batch([t for _, t in missing])
                        for (sid, _t), vec in zip(missing, new_vecs):
                            sentence_vec_by_id[sid] = vec
                            try:
                                db.execute(sql_text(
                                    "UPDATE article_sentence "
                                    "SET embedding = (:v)::vector "
                                    "WHERE sentence_id = :sid"
                                ), {"v": _fmt_pgvec(vec), "sid": sid})
                            except Exception as ue:
                                logger.warning(
                                    "build_and_cache_response: failed to persist sentence embedding sid=%d: %s",
                                    sid, ue,
                                )
                                db.rollback()
                        try:
                            db.commit()
                        except Exception as ce:
                            logger.warning(
                                "build_and_cache_response: commit of cached embeddings failed: %s", ce,
                            )
                            db.rollback()
                    except Exception as be:
                        # Fallback: per-sentence embed() if batched call failed.
                        logger.warning(
                            "build_and_cache_response: embed_batch failed (%s); falling back to per-sentence embed()",
                            be,
                        )
                        if embed:
                            for sid, t in missing:
                                try:
                                    sentence_vec_by_id[sid] = embed(t)
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug(
                    "build_and_cache_response: sentence-vec preload failed (%s); per-master embed() fallback active",
                    e,
                )

        # Batch-embed all master texts. master_vec_by_pid is in-memory only
        # (masters are derived from current on-chain state, not cached).
        master_vec_by_pid = {}
        if embed_batch and master_claims:
            try:
                texts = [m["text"] for m in master_claims]
                vecs = embed_batch(texts)
                for m, v in zip(master_claims, vecs):
                    master_vec_by_pid[int(m["post_id"])] = v
            except Exception as e:
                logger.warning(
                    "build_and_cache_response: batched master embedding failed (%s); falling back to per-master embed()",
                    e,
                )

        # Precompute section vectors ONCE for find_best_section reuse
        # across all unplaced masters in this rebuild. Section content
        # doesn't change between iterations.
        section_vec_by_id = {}
        if embed_batch and sections:
            try:
                section_texts_in_order = []
                section_ids_in_order = []
                for sec_id, sec_data in sections.items():
                    sec_text = sec_data["heading"] + ". " + " ".join(
                        s["text"] for s in sec_data["sentences"][:8]
                    )
                    section_ids_in_order.append(sec_id)
                    section_texts_in_order.append(sec_text)
                if section_texts_in_order:
                    sec_vecs = embed_batch(section_texts_in_order)
                    for sid, v in zip(section_ids_in_order, sec_vecs):
                        section_vec_by_id[sid] = v
            except Exception as e:
                logger.debug(
                    "build_and_cache_response: section-vec preload failed (%s); find_best_section will self-embed",
                    e,
                )

        to_hide_ids = []  # batched UPDATE at the end

        for master in master_claims:
            mpid = master["post_id"]
            mtext = master["text"]

            # Check if master is already in the article
            already_placed = False
            for sec in sections.values():
                for s in sec["sentences"]:
                    if s["post_id"] == mpid:
                        already_placed = True
                        break
                    # Also check if any group member is placed
                    if s["post_id"] in master["member_pids"]:
                        already_placed = True
                        break
                if already_placed:
                    break

            # Hide off-chain sentences similar to this master.
            if cosine_similarity:
                master_vec = master_vec_by_pid.get(int(mpid))
                # Per-master fallback if the batched call failed or this
                # master wasn't included for some reason.
                if master_vec is None and embed:
                    try:
                        master_vec = embed(mtext)
                    except Exception as e:
                        logger.debug("Master embedding failed for pid=%d: %s", mpid, e)
                        master_vec = None

                if master_vec is not None:
                    for sec in sections.values():
                        for s in sec["sentences"]:
                            if s["post_id"] is not None:
                                continue  # Don't hide on-chain
                            if s.get("_hidden"):
                                continue  # Already marked hidden by an earlier master
                            sid = int(s["sentence_id"])
                            sent_vec = sentence_vec_by_id.get(sid)
                            # Per-sentence fallback if the preload missed
                            # this sentence (e.g. preload was disabled).
                            if sent_vec is None and embed:
                                try:
                                    sent_vec = embed(s["text"])
                                    sentence_vec_by_id[sid] = sent_vec
                                except Exception:
                                    continue
                            if sent_vec is None:
                                continue
                            try:
                                sim = cosine_similarity(master_vec, sent_vec)
                            except Exception:
                                continue
                            if sim >= SIMILARITY_THRESHOLD:
                                to_hide_ids.append(sid)
                                s["_hidden"] = True
                                logger.debug(
                                    "Hid off-chain sid=%d (sim=%.2f to master %d)",
                                    sid, sim, mpid,
                                )

            # Insert master if not already placed.
            if not already_placed:
                from articles.claim_indexer import find_best_section
                sec_id = find_best_section(
                    db, article_id, mtext,
                    section_vec_by_id=section_vec_by_id or None,
                    claim_vec=master_vec_by_pid.get(int(mpid)),
                )
                if sec_id and sec_id in sections:
                    max_so = max((s["sort_order"] for s in sections[sec_id]["sentences"]), default=0)
                    new_sid = insert_sentence(db, sec_id, None, mtext)
                    update_sentence_post_id(db, new_sid, mpid)
                    sections[sec_id]["sentences"].append({
                        "sentence_id": new_sid, "sort_order": max_so + 100,
                        "text": mtext, "post_id": mpid,
                    })
                    logger.info("Placed master claim %d in section %d", mpid, sec_id)

        # Single batched UPDATE for hidden sentences (one short write
        # transaction instead of one per hidden sentence inside the loop).
        if to_hide_ids:
            try:
                # De-duplicate in case multiple masters flagged the same sentence.
                unique_ids = sorted(set(to_hide_ids))
                db.execute(sql_text(
                    "UPDATE article_sentence "
                    "SET is_hidden = TRUE "
                    "WHERE sentence_id = ANY(:ids)"
                ), {"ids": unique_ids})
                logger.info(
                    "build_and_cache_response: hid %d off-chain sentence(s) in article %d",
                    len(unique_ids), article_id,
                )
            except Exception as e:
                logger.warning(
                    "build_and_cache_response: batched is_hidden UPDATE failed: %s", e,
                )
                db.rollback()

        db.commit()

        # ── Step 4: Rebuild article dict with cleaned data ──
        # Remove hidden sentences and empty sections
        result_sections = []
        for sec_id in [s[0] for s in secs]:  # preserve original order
            if sec_id not in sections:
                continue
            sec = sections[sec_id]
            clean_sents = [s for s in sec["sentences"] if not s.get("_hidden")]
            if not clean_sents:
                continue

            # Enrich with VS/stake from DB
            from chain.chain_db import get_stake_totals, get_verity_score
            for s in clean_sents:
                pid = s.get("post_id")
                if pid is not None:
                    try:
                        sup, chal = get_stake_totals(db, pid)
                        s["stake_support"] = sup
                        s["stake_challenge"] = chal
                        s["verity_score"] = get_verity_score(db, pid)
                    except Exception:
                        s["stake_support"] = 0
                        s["stake_challenge"] = 0
                        s["verity_score"] = 0
                else:
                    s["stake_support"] = 0
                    s["stake_challenge"] = 0
                    s["verity_score"] = 0

                # Add dupe group info for frontend rollup
                if pid and s.get("post_id"):
                    dg_row = db.execute(sql_text(
                        "SELECT ct.dupe_group_id, g.member_count "
                        "FROM chain_claim_text ct "
                        "LEFT JOIN claim_dupe_group g ON ct.dupe_group_id = g.group_id "
                        "WHERE ct.post_id = :pid"
                    ), {"pid": pid}).fetchone()
                    if dg_row and dg_row[0]:
                        s["dupe_group_id"] = dg_row[0]
                        if dg_row[1] and dg_row[1] > 1:
                            s["dupe_count"] = dg_row[1]

            # Content moderation
            try:
                from moderation import check_content_fast
                for s in clean_sents:
                    mod = check_content_fast(s.get("text", ""))
                    if not mod.allowed:
                        s["text"] = "[Content hidden — policy violation]"
                        s["moderated"] = True
            except Exception:
                pass

            result_sections.append({
                "section_id": sec["section_id"],
                "heading": sec["heading"],
                "sentences": clean_sents,
            })

        article = {
            "article_id": article_id,
            "title": title,
            "topic_key": key,
            "sections": result_sections,
        }

        # ── Step 5: Cache ──
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
        logger.info("Cached article '%s': %d sections, hash=%s",
                     topic_key, len(result_sections), response_hash)

    except Exception as e:
        logger.warning("build_and_cache_response failed for '%s': %s", topic_key, e)
        import traceback
        traceback.print_exc()
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if owns:
            try:
                db.close()
            except Exception:
                pass


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
    
    Embeddings cached in article_sentence.embedding (pgvector vector(1536)).
    Each sentence is embedded at most once across all dedup runs."""
    import logging
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)
    try:
        # Step 1: load all non-hidden sentences. Cast embedding to text so
        # SQLAlchemy returns it as a string we can parse, since pgvector's
        # default Python adapter isn't installed.
        rows = db.execute(sql_text(
            "SELECT s.sentence_id, s.text, s.post_id, s.embedding::text "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "WHERE sec.article_id = :a AND s.is_hidden = FALSE "
            "ORDER BY s.section_id, s.sort_order"
        ), {"a": article_id}).fetchall()
        
        if not rows:
            return
        
        # Helper: parse pgvector text format "[0.1,0.2,...]" to list[float]
        def _parse_vec(s):
            if s is None:
                return None
            s = s.strip()
            if not s.startswith("["):
                return None
            return [float(x) for x in s[1:-1].split(",")]
        
        # Helper: format list[float] as pgvector literal
        def _fmt_vec(v):
            return "[" + ",".join(repr(float(x)) for x in v) + "]"
        
        # Step 2: identify sentences needing embedding
        needs_embed = [(r[0], r[1]) for r in rows if r[3] is None]
        embeddings_by_id = {}
        for r in rows:
            if r[3] is not None:
                embeddings_by_id[r[0]] = _parse_vec(r[3])
        
        if needs_embed:
            from embedding import embed_batch
            texts_to_embed = [t for (_, t) in needs_embed]
            logger.info("persist_dedup: embedding %d new sentences in article %d",
                        len(texts_to_embed), article_id)
            vecs = embed_batch(texts_to_embed)
            
            for (sid, _), vec in zip(needs_embed, vecs):
                embeddings_by_id[sid] = vec
                try:
                    db.execute(sql_text(
                        "UPDATE article_sentence SET embedding = (:v)::vector "
                        "WHERE sentence_id = :sid"
                    ), {"v": _fmt_vec(vec), "sid": sid})
                except Exception as e:
                    logger.warning("Failed to persist embedding for sid=%d: %s", sid, e)
                    db.rollback()
            db.commit()
        
        # Step 3: dedup decision using pgvector's cosine distance operator <=>
        # cosine_similarity = 1 - cosine_distance
        # Threshold: similarity >= 0.70 means cosine_distance <= 0.30
        DEDUP_DISTANCE = 0.20  # cosine distance; 0.20 = similarity 0.80
        
        # Identify on-chain sentences (always kept)
        onchain_ids = [r[0] for r in rows if r[2] is not None]
        offchain_rows = [r for r in rows if r[2] is None]
        
        to_hide = []
        kept_offchain_ids = []
        
        for r in offchain_rows:
            sid, text, _, _ = r
            text_s = (text or "").strip()
            if not text_s:
                to_hide.append(sid)
                continue
            
            # Use pgvector's <=> operator to find nearest neighbor among
            # on-chain + already-kept off-chain sentences within this article.
            # Only compare against other off-chain sentences.
            # On-chain masters are handled by build_and_cache_response.
            comparison_ids = kept_offchain_ids
            if not comparison_ids:
                kept_offchain_ids.append(sid)
                continue
            
            row = db.execute(sql_text(
                "SELECT MIN(s2.embedding <=> s1.embedding) AS dist "
                "FROM article_sentence s1, article_sentence s2 "
                "WHERE s1.sentence_id = :sid "
                "  AND s2.sentence_id = ANY(:cids) "
                "  AND s1.embedding IS NOT NULL "
                "  AND s2.embedding IS NOT NULL"
            ), {"sid": sid, "cids": comparison_ids}).fetchone()
            
            min_dist = row[0] if row and row[0] is not None else None
            if min_dist is not None and min_dist <= DEDUP_DISTANCE:
                to_hide.append(sid)
                logger.debug("dedup: hiding sid=%d (dist=%.3f)", sid, min_dist)
            else:
                kept_offchain_ids.append(sid)
        
        if to_hide:
            db.execute(sql_text(
                "UPDATE article_sentence SET is_hidden = TRUE "
                "WHERE sentence_id = ANY(:ids)"
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



def background_refresh_worker():
    """Autotuned background worker. Refreshes the oldest article continuously,
    sleeping between runs so the total cycle takes 24h regardless of N articles
    or per-refresh duration.
    
    sleep_time = max(0, (24h - N * avg_elapsed) / N)
    
    Single thread per app process. Idempotent on restart (resumes from
    last_refreshed_at ordering)."""
    import logging, threading, time, statistics
    from sqlalchemy import text as sql_text
    logger = logging.getLogger(__name__)
    CYCLE_SECONDS = 86400  # 24h target full cycle
    recent_elapsed = []  # rolling avg of last 10 refreshes
    
    def _worker():
        from db import get_session_factory
        Sess = get_session_factory()
        # Initial sleep to avoid hitting LLM during app startup
        time.sleep(60)
        
        while True:
            try:
                db = Sess()
                # Pick the oldest article by last_refreshed_at (NULLs first)
                row = db.execute(sql_text(
                    "SELECT topic_key, article_id FROM topic_article "
                    "ORDER BY last_refreshed_at NULLS FIRST LIMIT 1"
                )).fetchone()
                
                # Count for autotune
                n_row = db.execute(sql_text(
                    "SELECT COUNT(*) FROM topic_article"
                )).fetchone()
                N = n_row[0] if n_row else 1
                db.close()
                
                if not row:
                    time.sleep(300)
                    continue
                
                topic_key, article_id = row
                t0 = time.time()
                try:
                    refresh_article(Sess(), topic_key)
                    persist_dedup(Sess(), article_id)
                    build_and_cache_response(Sess, topic_key)
                    elapsed = time.time() - t0
                    recent_elapsed.append(elapsed)
                    if len(recent_elapsed) > 10:
                        recent_elapsed.pop(0)
                    logger.info("bg-refresh: '%s' done in %.1fs (avg %.1fs over %d runs)",
                                topic_key, elapsed,
                                statistics.mean(recent_elapsed), len(recent_elapsed))
                except Exception as e:
                    logger.warning("bg-refresh failed for '%s': %s", topic_key, e)
                    elapsed = time.time() - t0
                
                # Autotune sleep: distribute remaining 24h budget across N articles
                avg_elapsed = statistics.mean(recent_elapsed) if recent_elapsed else 30.0
                sleep_time = max(60, (CYCLE_SECONDS - N * avg_elapsed) / max(N, 1))
                logger.info("bg-refresh: sleeping %.0fs before next refresh (N=%d, avg=%.1fs)",
                            sleep_time, N, avg_elapsed)
                time.sleep(sleep_time)
            except Exception as e:
                logger.warning("bg-refresh worker error: %s", e)
                time.sleep(60)
    
    t = threading.Thread(target=_worker, daemon=True, name="bg-article-refresh")
    t.start()
    logger.info("Background article refresh worker started")
