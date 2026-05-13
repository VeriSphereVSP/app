#!/usr/bin/env python3
"""Standalone background worker for chain indexing, article refresh, and dupe groups.
Runs as a separate process so it never blocks the API server."""

import asyncio
import time
import sys
import os
import logging

# patch05a: enable INFO-level logging so logger.info / logger.warning
# calls from dupe_groups, chain_indexer, and other modules are
# visible in `docker compose logs worker`. Without this, Python's
# default WARNING-only level silently dropped all operational
# logging, making debugging extremely difficult.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

# Ensure app directory is in path
sys.path.insert(0, os.path.dirname(__file__))

async def main():
    print("=== Verisphere Background Worker ===")

    # patch04a: legacy indexer disabled.
    # app/chain/indexer.py is kept on disk for reference but is no
    # longer instantiated — its _sync_posts loop iterates from
    # pid=0 (off-by-one) and races with the main indexer's
    # claim-row update path, producing rows with claim.post_id=0.
    # The main indexer in chain_indexer.py covers all of its
    # functionality plus more.
    from chain_indexer import start_indexer

    start_indexer()
    print("Chain indexer started (legacy run_indexer disabled by patch04a)")

    # Periodic dupe group refresh (every 5 minutes)
    async def _dupe_refresh():
        await asyncio.sleep(180)  # Initial delay
        while True:
            try:
                from db import get_session_factory
                from dupe_groups import refresh_all_groups
                sess = get_session_factory()()
                try:
                    refresh_all_groups(sess)
                finally:
                    sess.close()
            except Exception as e:
                print(f"Dupe group refresh error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(_dupe_refresh())
    print("Dupe group refresh scheduled")

    # patch04: one-shot topic backfill at startup.
    # Finds chain_claim_text rows whose corresponding claim has no
    # topic, and runs detect_topic for each. Catches any posts that
    # were indexed before patch04 landed, or any posts indexed while
    # the LLM provider was temporarily unavailable.
    async def _topic_backfill():
        await asyncio.sleep(30)  # let indexer's full_sync settle
        try:
            from db import get_session_factory
            from sqlalchemy import text as sql_text
            from articles.topic_detect import detect_topic, ensure_article_for_claim
            from semantic import ensure_claim
            sess = get_session_factory()()
            try:
                rows = sess.execute(sql_text(
                    "SELECT ct.post_id, ct.claim_text "
                    "FROM chain_claim_text ct "
                    "LEFT JOIN claim c ON c.post_id = ct.post_id "
                    "WHERE c.topic IS NULL OR c.topic = '' "
                    "ORDER BY ct.post_id"
                )).fetchall()
                if not rows:
                    print("topic-backfill: no posts need topic")
                    return
                print(f"topic-backfill: {len(rows)} post(s) need topic")
                done = 0
                for pid, text in rows:
                    try:
                        cid = ensure_claim(sess, text)
                        topic = detect_topic(text)
                        if topic:
                            sess.execute(sql_text(
                                "UPDATE claim SET topic = :t "
                                "WHERE claim_id = :cid AND (topic IS NULL OR topic = '')"
                            ), {"t": topic, "cid": cid})
                            sess.commit()
                            try:
                                ensure_article_for_claim(sess, text, pid, topic)
                            except Exception as ex:
                                print(f"topic-backfill: ensure_article for post {pid} failed: {ex}")
                            done += 1
                            print(f"topic-backfill: post {pid} → {topic!r}")
                    except Exception as ex:
                        print(f"topic-backfill: post {pid} failed: {ex}")
                print(f"topic-backfill: complete ({done}/{len(rows)} succeeded)")
            finally:
                sess.close()
        except Exception as e:
            print(f"topic-backfill: top-level error: {e}")

    asyncio.create_task(_topic_backfill())
    print("Topic backfill scheduled")

    # Background article refresh
    async def _daily_refresh():
        import statistics
        from db import get_session_factory
        from articles.article_store import refresh_article, persist_dedup, build_and_cache_response
        from sqlalchemy import text as sql_text
        CYCLE_SECONDS = 86400  # 24h target
        recent_elapsed = []
        await asyncio.sleep(120)  # Initial delay
        while True:
            try:
                Sess = get_session_factory()
                db = Sess()
                try:
                    row = db.execute(sql_text(
                        "SELECT article_id, topic_key FROM topic_article "
                        "ORDER BY last_refreshed_at ASC NULLS FIRST LIMIT 1"
                    )).fetchone()
                    if not row:
                        await asyncio.sleep(60)
                        continue
                    aid, topic = row
                    t0 = time.time()
                    refresh_article(db, topic)
                    persist_dedup(db, aid)
                    build_and_cache_response(db, topic)
                    elapsed = time.time() - t0
                    recent_elapsed.append(elapsed)
                    if len(recent_elapsed) > 20:
                        recent_elapsed = recent_elapsed[-20:]
                    # Count total articles
                    total = db.execute(sql_text(
                        "SELECT count(*) FROM topic_article"
                    )).scalar() or 1
                    avg = statistics.mean(recent_elapsed) if recent_elapsed else 30
                    gap = max((CYCLE_SECONDS / total) - avg, 5)
                    print(f"Refreshed article '{topic}' in {elapsed:.1f}s, next in {gap:.0f}s")
                    await asyncio.sleep(gap)
                finally:
                    db.close()
            except Exception as e:
                print(f"Article refresh error: {e}")
                await asyncio.sleep(60)

    asyncio.create_task(_daily_refresh())
    print("Article refresh scheduled")

    # patch04b: keep-alive loop. The main indexer runs in a native
    # thread via start_indexer() and doesn't need to be awaited.
    # We just need to keep the asyncio event loop alive so the
    # background tasks scheduled above (dupe_refresh, topic_backfill,
    # daily_refresh) can run their initial sleeps and cycles.
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("Worker shutting down")

if __name__ == "__main__":
    asyncio.run(main())
