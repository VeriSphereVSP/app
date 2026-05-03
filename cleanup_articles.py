"""One-shot cleanup of historical pollution in the article corpus.

Iterates every article in topic_article and runs
cleanup_irrelevant_claims_from_article() on it. Safe to re-run; cleanup
is idempotent (an article with no remaining pollution returns 0).

Run inside the app container:

    docker compose exec app python cleanup_articles.py
    docker compose exec app python cleanup_articles.py --topic 'climate change'
    docker compose exec app python cleanup_articles.py --dry-run

Each article is processed in its own session, so a failure on one
article does not affect any other article. Embedding API calls run
outside the write transaction, so concurrent worker activity does not
deadlock with this script.
"""
import argparse
import logging
import sys
import time
from typing import List, Optional, Tuple

from sqlalchemy import text as sql_text

from db import get_session_factory
from articles.claim_indexer import (
    cleanup_irrelevant_claims_from_article,
    is_claim_relevant_to_article,
    _article_summary_embedding,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("cleanup_articles")


def list_articles(topic: Optional[str]) -> List[Tuple[int, str, str]]:
    """Return [(article_id, topic_key, title), ...] for every article
    we should consider."""
    Session = get_session_factory()
    db = Session()
    try:
        if topic:
            rows = db.execute(sql_text(
                "SELECT article_id, topic_key, title FROM topic_article "
                "WHERE LOWER(topic_key) = LOWER(:t) "
                "ORDER BY article_id"
            ), {"t": topic}).fetchall()
        else:
            rows = db.execute(sql_text(
                "SELECT article_id, topic_key, title FROM topic_article "
                "ORDER BY article_id"
            )).fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]
    finally:
        db.close()


def dry_run_one(article_id: int, topic_key: str) -> int:
    """Report (without deleting) how many sentences would be removed."""
    Session = get_session_factory()
    db = Session()
    try:
        article_vec = _article_summary_embedding(db, article_id)
        if article_vec is None:
            log.warning(
                "Article %d (%s): summary embedding unavailable; would skip",
                article_id, topic_key,
            )
            return 0
        rows = db.execute(sql_text(
            "SELECT s.sentence_id, c.claim_text "
            "FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN claim c ON c.post_id = s.post_id "
            "WHERE sec.article_id = :a "
            "  AND s.post_id IS NOT NULL "
            "  AND LOWER(TRIM(s.text)) = LOWER(TRIM(c.claim_text))"
        ), {"a": article_id}).fetchall()
        n = 0
        for sentence_id, claim_text in rows:
            if not is_claim_relevant_to_article(
                db, article_id, claim_text, article_vec=article_vec,
            ):
                n += 1
                log.info(
                    "  WOULD remove sentence %s: %s",
                    sentence_id, (claim_text or "")[:80],
                )
        return n
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--topic", help="Cleanup only the article with this topic_key (case-insensitive).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be removed without deleting.")
    p.add_argument("--rebuild", action="store_true",
                   help="After cleanup, also rebuild each affected article's "
                        "cached_response so the change is immediately visible "
                        "via the API.")
    args = p.parse_args()

    articles = list_articles(args.topic)
    if not articles:
        log.warning("No matching articles found.")
        return 0

    log.info("Processing %d article(s)", len(articles))
    total_removed = 0
    affected: List[Tuple[int, str]] = []
    Session = get_session_factory()

    for article_id, topic_key, title in articles:
        try:
            if args.dry_run:
                n = dry_run_one(article_id, topic_key)
            else:
                n = cleanup_irrelevant_claims_from_article(
                    Session, article_id,
                )
            if n:
                total_removed += n
                affected.append((article_id, topic_key))
                log.info(
                    "Article %d (%s): %s %d sentence(s)",
                    article_id, topic_key,
                    "would remove" if args.dry_run else "removed", n,
                )
            else:
                log.info("Article %d (%s): clean", article_id, topic_key)
        except Exception as e:
            log.error(
                "Article %d (%s): cleanup raised %s: %s",
                article_id, topic_key, type(e).__name__, e,
            )

    log.info("─" * 60)
    log.info(
        "Done. %s %d sentence(s) across %d article(s).",
        "Would remove" if args.dry_run else "Removed",
        total_removed, len(affected),
    )

    if args.rebuild and not args.dry_run and affected:
        log.info("Rebuilding cached_response for %d affected article(s)...",
                 len(affected))
        from articles.article_store import build_and_cache_response
        for article_id, topic_key in affected:
            try:
                build_and_cache_response(Session, topic_key)
                log.info("Rebuilt article %d (%s)", article_id, topic_key)
            except Exception as e:
                log.error(
                    "Rebuild of article %d (%s) failed: %s",
                    article_id, topic_key, e,
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
