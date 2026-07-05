"""Derived-state worker.

Background thread that processes the `derived_state_queue` table. The
chain indexer enqueues a row whenever a claim post is created or
updated; this worker reads them and runs the slow derivation work
(dupe-grouping, topic detection, article-system updates) without
blocking the indexer's poll loop.

Failures are retried with exponential backoff up to MAX_ATTEMPTS, after
which the row is marked 'failed' for manual review.

Started from worker.py alongside the chain indexer.
"""

import logging
import threading
import time
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_session_factory

logger = logging.getLogger(__name__)

# ── Tuning ─────────────────────────────────────────────────────────
POLL_INTERVAL_BUSY = 1.0     # seconds between polls when queue had work
POLL_INTERVAL_IDLE = 5.0     # seconds between polls when queue was empty
BATCH_SIZE         = 20      # rows claimed per cycle
MAX_ATTEMPTS       = 10
BACKOFF_BASE_SEC   = 2.0     # exponential: 2, 4, 8, 16, 32, 64, 128, 256, 512, 600 (capped)
BACKOFF_MAX_SEC    = 600     # 10 minutes
STALE_INPROGRESS_MIN = 5     # rows stuck in_progress > this many minutes get reclaimed

_worker_thread = None


# ── The slow work ──────────────────────────────────────────────────

def _do_derived_state(db: Session, post_id: int, queue_kind: str) -> None:
    """Run dupe-grouping, topic detection, article-system updates for a post.
    Commits its own transaction. Raises on failure (caller handles retry)."""
    row = db.execute(sql_text(
        "SELECT claim_text FROM chain_claim_text WHERE post_id = :p"
    ), {"p": post_id}).fetchone()
    if not row or not row[0]:
        logger.debug("derived_state_worker: no claim_text for post %d (link?), skipping", post_id)
        return  # not a claim (links don't have claim text), nothing to derive
    claim_text = row[0]

    # ── Dupe groups ────────────────────────────────────────────────
    try:
        from dupe_groups import embed_claim, assign_to_group
        embed_claim(db, post_id, claim_text)
        assign_to_group(db, post_id)
        db.commit()
    except Exception as e:
        logger.warning("derived_state_worker: dupe-grouping failed for post %d: %s", post_id, e)
        db.rollback()
        raise

    # ── Topic detection + article skeleton ─────────────────────────
    try:
        from semantic import ensure_claim
        from articles.topic_detect import detect_topic, ensure_article_for_claim, snap_topic  # patch_topic_snap
        cid = ensure_claim(db, claim_text)
        existing = db.execute(sql_text(
            "SELECT topic FROM claim WHERE claim_id = :c"
        ), {"c": cid}).fetchone()
        if not existing or not existing[0]:
            # patch_topic_snap: cluster-then-label. Inherit the topic of a
            # near-identical existing claim (cosine >= VSP_TOPIC_SNAP_THRESHOLD)
            # so near-duplicate claims share a topic and can group; fall back to
            # the LLM classifier only when there is no strong neighbour.
            topic = snap_topic(db, post_id) or detect_topic(claim_text)
            if topic:
                db.execute(sql_text(
                    "UPDATE claim SET topic = :t WHERE claim_id = :c"
                ), {"t": topic, "c": cid})
                # patch_derived_signature_fix: ensure_article_for_claim signature
                # is (db, claim_text, post_id, topic) — 4 args. Previous call
                # passed only 3 and broke every new-claim topic-detect.
                ensure_article_for_claim(db, claim_text, post_id, topic)
        db.commit()
    except Exception as e:
        logger.warning("derived_state_worker: topic detection failed for post %d: %s", post_id, e)
        db.rollback()
        raise

    # ── Article system: insert into best section + cross-index ────
    try:
        from articles.article_store import apply_new_post
        from articles.claim_indexer import cross_index_claim_into_all_articles
        apply_new_post(db, post_id, claim_text)
        cross_index_claim_into_all_articles(db, claim_text, post_id)
    except Exception as e:
        logger.warning("derived_state_worker: article-system failed for post %d: %s", post_id, e)
        db.rollback()
        raise


# ── Batch processing ───────────────────────────────────────────────

def _claim_batch(db: Session, batch_size: int):
    """Atomically claim up to batch_size pending rows by flipping their
    status to in_progress. Uses SKIP LOCKED so multiple workers (future)
    don't double-claim. Returns the claimed rows."""
    # The `started_at` predicate handles two cases:
    #   (a) rows that have never been started: started_at IS NULL
    #   (b) rows on backoff: started_at is a *future* time and we wait
    #   (c) rows stuck in_progress from a crashed worker: started_at is
    #       in the past by more than STALE_INPROGRESS_MIN minutes; we
    #       reclaim them. (status is still 'in_progress' but we treat
    #       it as recoverable.)
    rows = db.execute(sql_text(f"""
        UPDATE derived_state_queue
        SET    status        = 'in_progress',
               started_at    = now(),
               attempt_count = attempt_count + 1
        WHERE  id IN (
            SELECT id FROM derived_state_queue
            WHERE  (
                status = 'pending'
                AND (started_at IS NULL OR started_at <= now())
            ) OR (
                status = 'in_progress'
                AND started_at < now() - INTERVAL '{STALE_INPROGRESS_MIN} minutes'
            )
            ORDER BY queued_at
            LIMIT :n
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, post_id, queue_kind, attempt_count
    """), {"n": batch_size}).fetchall()
    db.commit()
    return rows


def _mark_completed(db: Session, row_id: int) -> None:
    db.execute(sql_text("""
        UPDATE derived_state_queue
        SET    status       = 'completed',
               completed_at = now(),
               last_error   = NULL
        WHERE  id = :id
    """), {"id": row_id})
    db.commit()


def _mark_failed(db: Session, row_id: int, err_msg: str) -> None:
    db.execute(sql_text("""
        UPDATE derived_state_queue
        SET    status       = 'failed',
               completed_at = now(),
               last_error   = :err
        WHERE  id = :id
    """), {"id": row_id, "err": err_msg[:1000]})
    db.commit()


def _requeue_with_backoff(db: Session, row_id: int, attempt: int, err_msg: str) -> int:
    """Mark a row pending again with a future started_at to gate the retry.
    Returns the backoff seconds applied."""
    backoff = min(BACKOFF_BASE_SEC ** attempt, BACKOFF_MAX_SEC)
    db.execute(sql_text("""
        UPDATE derived_state_queue
        SET    status     = 'pending',
               last_error = :err,
               started_at = now() + (:backoff || ' seconds')::interval
        WHERE  id = :id
    """), {"id": row_id, "err": err_msg[:1000], "backoff": int(backoff)})
    db.commit()
    return int(backoff)


def process_one_batch(db: Session, batch_size: int = BATCH_SIZE) -> bool:
    """Claim and process one batch. Returns True if any rows were processed."""
    rows = _claim_batch(db, batch_size)
    if not rows:
        return False

    for row in rows:
        try:
            _do_derived_state(db, row.post_id, row.queue_kind)
            _mark_completed(db, row.id)
        except Exception as e:
            err_msg = str(e)
            if row.attempt_count >= MAX_ATTEMPTS:
                logger.error(
                    "derived_state_worker: row %d (post_id=%d) FAILED after %d attempts: %s",
                    row.id, row.post_id, row.attempt_count, err_msg)
                _mark_failed(db, row.id, err_msg)
            else:
                backoff = _requeue_with_backoff(db, row.id, row.attempt_count, err_msg)
                logger.warning(
                    "derived_state_worker: row %d (post_id=%d) retry %d/%d after %ds: %s",
                    row.id, row.post_id, row.attempt_count, MAX_ATTEMPTS, backoff, err_msg)
    return True


# ── Thread management ──────────────────────────────────────────────

def _run():
    logger.info("Derived-state worker starting...")
    while True:
        did_work = False
        try:
            db = get_session_factory()()
            try:
                did_work = process_one_batch(db)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Derived-state worker outer error: %s", e)
        time.sleep(POLL_INTERVAL_BUSY if did_work else POLL_INTERVAL_IDLE)


def start_derived_state_worker():
    """Start the background derived-state worker thread."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=_run, daemon=True, name="derived-state-worker")
    _worker_thread.start()
    logger.info("Derived-state worker thread started")
