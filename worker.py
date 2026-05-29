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
    # patch_bundle04_atomic: start derived-state worker alongside indexer.
    # Indexer writes canonical chain state synchronously; derived-state
    # worker chews through the slow article-system/dupe-grouping/topic
    # detection work asynchronously from derived_state_queue.
    from chain_indexer import start_indexer
    from derived_state_worker import start_derived_state_worker

    start_indexer()
    print("Chain indexer started (legacy run_indexer disabled by patch04a)")

    start_derived_state_worker()
    print("Derived-state worker started")

    # patch_bundle04_p2: periodic drift-check between DB and chain.
    from indexer_audit import start_indexer_audit
    start_indexer_audit()
    print("Indexer audit started")

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

    # patch_bundle04_atomic: _topic_backfill removed.
    # full_sync at startup now enqueues derived-state work for every
    # claim post; the derived-state worker handles topic detection
    # uniformly with the rest of the derivation pipeline. No separate
    # backfill task needed.

    # Background article refresh
    async def _daily_refresh():
        import statistics
        from db import get_session_factory
        from articles.article_store import refresh_article, persist_dedup, build_and_cache_response
        from sqlalchemy import text as sql_text
        CYCLE_SECONDS = 86400  # 24h target
        recent_elapsed = []
        await asyncio.sleep(120)  # Initial delay
        # patch_bundle04_5_p7_worker_session_leak: session must be opened, used, and closed
        # ENTIRELY BEFORE any `await asyncio.sleep(...)`. Holding a session
        # across an async sleep, while it has an open (implicit) transaction
        # from a prior SELECT, is what produced the idle-in-transaction
        # leak on `SELECT count(*) FROM topic_article` (bounded by the 5-min
        # idle_in_transaction_session_timeout, but leaking real connections
        # every cycle until then).
        while True:
            try:
                # Compute next sleep INSIDE the DB-work block and use it
                # AFTER db.close(). The two paths (no-article vs. refreshed)
                # set `next_sleep` independently; we sleep once below.
                next_sleep = 60  # default if anything unexpected
                Sess = get_session_factory()
                db = Sess()
                try:
                    row = db.execute(sql_text(
                        "SELECT article_id, topic_key FROM topic_article "
                        "ORDER BY last_refreshed_at ASC NULLS FIRST LIMIT 1"
                    )).fetchone()
                    if not row:
                        # No articles yet — just poll again in 60s.
                        next_sleep = 60
                    else:
                        aid, topic = row
                        t0 = time.time()
                        refresh_article(db, topic)
                        persist_dedup(db, aid)
                        build_and_cache_response(db, topic)
                        elapsed = time.time() - t0
                        recent_elapsed.append(elapsed)
                        if len(recent_elapsed) > 20:
                            recent_elapsed = recent_elapsed[-20:]
                        total = db.execute(sql_text(
                            "SELECT count(*) FROM topic_article"
                        )).scalar() or 1
                        avg = statistics.mean(recent_elapsed) if recent_elapsed else 30
                        gap = max((CYCLE_SECONDS / total) - avg, 5)
                        print(f"Refreshed article '{topic}' in {elapsed:.1f}s, next in {gap:.0f}s")
                        next_sleep = gap
                    # Commit closes any implicit transaction opened by the
                    # SELECTs above. The article-store helpers above commit
                    # their own writes; this catches the read-only SELECTs.
                    db.commit()
                finally:
                    db.close()
                # Session is closed here. Safe to sleep without holding
                # an idle-in-transaction session.
                await asyncio.sleep(next_sleep)
            except Exception as e:
                print(f"Article refresh error: {e}")
                await asyncio.sleep(60)

    asyncio.create_task(_daily_refresh())
    print("Article refresh scheduled")

    # patch04b: keep-alive loop. The main indexer runs in a native
    # thread via start_indexer() and doesn't need to be awaited.
    # We just need to keep the asyncio event loop alive so the
    # background tasks scheduled above (dupe_refresh, daily_refresh)
    # can run their initial sleeps and cycles.
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        print("Worker shutting down")

if __name__ == "__main__":
    asyncio.run(main())
