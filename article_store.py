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