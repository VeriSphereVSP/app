#!/usr/bin/env python3
"""Standalone background worker for chain indexing, article refresh, and dupe groups.
Runs as a separate process so it never blocks the API server."""

import asyncio
import time
import sys
import os

# Ensure app directory is in path
sys.path.insert(0, os.path.dirname(__file__))

async def main():
    print("=== Verisphere Background Worker ===")

    from chain.indexer import run_indexer
    from chain_indexer import start_indexer

    # Start chain indexer
    indexer_task = asyncio.create_task(run_indexer())
    start_indexer()
    print("Chain indexer started")

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
                    refresh_article(db, aid)
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

    # Keep running
    try:
        await indexer_task
    except asyncio.CancelledError:
        print("Worker shutting down")

if __name__ == "__main__":
    asyncio.run(main())
