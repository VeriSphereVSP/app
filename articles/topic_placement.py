#!/usr/bin/env python3
"""topic_placement.py — Build B (safe slice): project claim_topic -> article placement.

For every claim_topic association whose topic has an article, ensure a sentence
carrying the claim's post_id exists in that article. ADDITIVE ONLY — inserts what's
missing, never resets or removes existing attachments, so it does not touch the
fragile overlay reset/reattach loop. Run as a manual/cadence pass (like heal); it
backstops any claim the overlay left unplaced.

NOTE: this is eventually-consistent hardening. A HARD guarantee (the overlay itself
always attaching per claim_topic) is the deeper overlay change, intentionally
deferred. Dry-run first.

CLI:
  python -m articles.topic_placement project --dry-run   # list missing placements
  python -m articles.topic_placement project             # insert them
"""
from __future__ import annotations
import logging, sys
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
logger = logging.getLogger(__name__)


def project_placements(db: Session, article_id=None, dry_run: bool = True):
    missing = db.execute(sql_text(
        "SELECT DISTINCT x.post_id, cl.claim_text, ta.article_id, t.label "
        "FROM claim_topic ct "
        "JOIN claim cl ON cl.claim_id = ct.claim_id "
        "JOIN chain_claim_text x ON x.claim_text = cl.claim_text AND x.post_id IS NOT NULL "
        "JOIN topic t ON t.topic_id = ct.topic_id AND t.status = 'active' "
        "JOIN topic_article ta ON lower(ta.topic_key) = lower(t.label) "
        "                      OR lower(ta.title) = lower(t.label) "
        "WHERE (:aid IS NULL OR ta.article_id = :aid) "
        "  AND NOT EXISTS (SELECT 1 FROM article_sentence s "
        "                  JOIN article_section sec ON s.section_id = sec.section_id "
        "                  WHERE sec.article_id = ta.article_id AND s.post_id = x.post_id)"
    ), {"aid": article_id}).fetchall()

    placed = 0
    if not dry_run:
        from articles.article_store import insert_sentence
        from articles.claim_indexer import find_best_section
        for post_id, claim_text, aid, _label in missing:
            try:
                sec_id = find_best_section(db, aid, claim_text)
                if not sec_id:
                    continue
                last = db.execute(sql_text(
                    "SELECT sentence_id FROM article_sentence WHERE section_id=:s "
                    "ORDER BY sort_order DESC LIMIT 1"), {"s": sec_id}).fetchone()
                new_sid = insert_sentence(db, sec_id, last[0] if last else None, claim_text)
                db.execute(sql_text(
                    "UPDATE article_sentence SET post_id=:p WHERE sentence_id=:s"),
                    {"p": post_id, "s": new_sid})
                placed += 1
            except Exception as e:
                logger.warning("project placement failed (post %s, art %s): %s", post_id, aid, e)
        db.commit()
    return placed, missing


def _main(argv):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    dry = "--dry-run" in argv
    with sessionmaker(bind=create_engine(DATABASE_URL, future=True))() as db:
        placed, missing = project_placements(db, dry_run=dry)
        head = "DRY-RUN — missing placements (no writes):" if dry else f"PLACED {placed}:"
        print(f"[placement] {head}")
        for post_id, _txt, aid, label in missing:
            print(f"  post {post_id} -> article {aid} ({label})")
        print(f"[placement] {len(missing)} claim(s) not yet placed in an associated article")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(_main(sys.argv))
