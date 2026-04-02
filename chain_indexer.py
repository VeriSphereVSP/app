# app/chain_indexer.py
"""
Chain Indexer — watches on-chain events and syncs state to the DB.

Runs as a background thread inside the app process.
Polls for new events every POLL_INTERVAL seconds.
On each relevant event, reads current on-chain state and writes to DB.

Tables populated:
  chain_post         — per-post stake totals, VS, activity status
  chain_user_stake   — per-user per-post stake positions
  chain_link         — evidence links from LinkGraph
  chain_claim_text   — claim text from PostRegistry
  chain_global       — sMax and other global stats
  chain_indexer_state — last processed block per contract
"""

import json
import logging
import threading
import time
from pathlib import Path

from web3 import Web3
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from db import get_session_factory
from config import (
    RPC_URL,
    POST_REGISTRY_ADDRESS,
    STAKE_ENGINE_ADDRESS,
    SCORE_ENGINE_ADDRESS,
    LINK_GRAPH_ADDRESS,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 10  # seconds
BLOCK_BATCH = 2000  # blocks per poll

# ── Web3 setup ────────────────────────────────────────

_w3 = None

def _get_w3():
    global _w3
    if _w3 is None:
        _w3 = Web3(Web3.HTTPProvider(RPC_URL))
    return _w3


def _load_abi(name):
    path = Path(f"/core/out/{name}.sol/{name}.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)["abi"]
    return []


# ── Index a single post ─────────────────────────────────

def index_post(db: Session, post_id: int, user_addresses: list[str] | None = None):
    """Read on-chain state for a post and upsert into DB.
    Optionally index specific user positions."""
    w3 = _get_w3()

    se_abi = _load_abi("StakeEngine")
    sc_abi = _load_abi("ScoreEngine")
    reg_abi = _load_abi("PostRegistry")

    se = w3.eth.contract(address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=se_abi)
    sc = w3.eth.contract(address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS), abi=sc_abi)
    reg = w3.eth.contract(address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=reg_abi)

    try:
        # Stake totals
        support_wei, challenge_wei = se.functions.getPostTotals(post_id).call()
        support = support_wei / 1e18
        challenge = challenge_wei / 1e18
        total = support + challenge

        # VS
        try:
            vs_ray = sc.functions.effectiveVSRay(post_id).call()
            effective_vs = (vs_ray / 1e18) * 100
        except Exception:
            effective_vs = 0.0

        try:
            base_ray = sc.functions.baseVSRay(post_id).call()
            base_vs = (base_ray / 1e18) * 100
        except Exception:
            base_vs = 0.0

        # Post metadata
        try:
            post_data = reg.functions.getPost(post_id).call()
            creator = post_data[0]
            content_type = post_data[2]  # 0=claim, 1=link (3rd field)
            created_epoch = post_data[1]
        except Exception:
            creator = None
            content_type = 0
            created_epoch = None

        # Activity check (total >= posting fee = 1 VSP)
        is_active = total >= 1.0

        db.execute(sql_text("""
            INSERT INTO chain_post (post_id, content_type, creator, support_total, challenge_total,
                                    base_vs, effective_vs, is_active, created_epoch, indexed_at)
            VALUES (:pid, :ct, :cr, :s, :c, :bvs, :evs, :active, :epoch, now())
            ON CONFLICT (post_id) DO UPDATE SET
                support_total = :s, challenge_total = :c,
                base_vs = :bvs, effective_vs = :evs,
                is_active = :active, indexed_at = now()
        """), {
            "pid": post_id, "ct": content_type, "cr": creator,
            "s": support, "c": challenge,
            "bvs": base_vs, "evs": effective_vs,
            "active": is_active, "epoch": created_epoch,
        })

        # Claim text (for claims only)
        if content_type == 0:
            try:
                # getPost returns (creator, timestamp, contentType, contentId, fee)
                content_id = post_data[3]
                claim_text = reg.functions.getClaim(content_id).call()
                db.execute(sql_text("""
                    INSERT INTO chain_claim_text (post_id, claim_text, indexed_at)
                    VALUES (:pid, :txt, now())
                    ON CONFLICT (post_id) DO UPDATE SET claim_text = :txt, indexed_at = now()
                """), {"pid": post_id, "txt": claim_text})
            except Exception as e:
                logger.debug("Could not index claim text for post %d: %s", post_id, e)

        # User positions
        if user_addresses:
            for addr in user_addresses:
                _index_user_stake(db, se, addr, post_id)

        db.commit()

    except Exception as e:
        logger.warning("Failed to index post %d: %s", post_id, e)
        db.rollback()


def _index_user_stake(db: Session, se, user_address: str, post_id: int):
    """Index a user's stake position on a post."""
    addr = Web3.to_checksum_address(user_address)
    for side in (0, 1):
        try:
            lot_info = se.functions.getUserLotInfo(addr, post_id, side).call()
            amount = lot_info[0] / 1e18
            weighted_pos = lot_info[1] / 1e18
            entry_epoch = lot_info[2]
            tranche = lot_info[4]
            pos_weight = lot_info[5] / 1e18

            if amount > 0:
                db.execute(sql_text("""
                    INSERT INTO chain_user_stake
                        (user_address, post_id, side, amount, weighted_position,
                         entry_epoch, tranche, position_weight, indexed_at)
                    VALUES (:addr, :pid, :side, :amt, :wp, :ee, :tr, :pw, now())
                    ON CONFLICT (user_address, post_id, side) DO UPDATE SET
                        amount = :amt, weighted_position = :wp,
                        tranche = :tr, position_weight = :pw, indexed_at = now()
                """), {
                    "addr": user_address.lower(), "pid": post_id, "side": side,
                    "amt": amount, "wp": weighted_pos, "ee": entry_epoch,
                    "tr": tranche, "pw": pos_weight,
                })
            else:
                db.execute(sql_text("""
                    DELETE FROM chain_user_stake
                    WHERE user_address = :addr AND post_id = :pid AND side = :side
                """), {"addr": user_address.lower(), "pid": post_id, "side": side})

        except Exception as e:
            logger.debug("Failed to index user stake %s post %d side %d: %s",
                         user_address[:10], post_id, side, e)


def index_link(db: Session, link_post_id: int, from_post_id: int,
               to_post_id: int, is_challenge: bool):
    """Index an evidence link."""
    db.execute(sql_text("""
        INSERT INTO chain_link (link_post_id, from_post_id, to_post_id, is_challenge, indexed_at)
        VALUES (:lpid, :fpid, :tpid, :ic, now())
        ON CONFLICT (link_post_id) DO UPDATE SET
            from_post_id = :fpid, to_post_id = :tpid,
            is_challenge = :ic, indexed_at = now()
    """), {"lpid": link_post_id, "fpid": from_post_id, "tpid": to_post_id, "ic": is_challenge})
    db.commit()


def index_global_stats(db: Session):
    """Index global protocol stats like sMax."""
    w3 = _get_w3()
    se_abi = _load_abi("StakeEngine")
    se = w3.eth.contract(address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=se_abi)

    try:
        s_max_wei = se.functions.sMax().call()
        s_max = s_max_wei / 1e18
        db.execute(sql_text("""
            INSERT INTO chain_global (key, value_num, updated_at)
            VALUES ('s_max', :val, now())
            ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
        """), {"val": s_max})

        num_tranches = se.functions.numTranches().call()
        db.execute(sql_text("""
            INSERT INTO chain_global (key, value_num, updated_at)
            VALUES ('num_tranches', :val, now())
            ON CONFLICT (key) DO UPDATE SET value_num = :val, updated_at = now()
        """), {"val": num_tranches})

        db.commit()
    except Exception as e:
        logger.warning("Failed to index global stats: %s", e)
        db.rollback()


# ── Full sync ───────────────────────────────────────────

def full_sync(db: Session):
    """Sync all posts from chain to DB. Run at startup."""
    w3 = _get_w3()
    reg_abi = _load_abi("PostRegistry")
    lg_abi = _load_abi("LinkGraph")
    reg = w3.eth.contract(address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=reg_abi)
    lg = w3.eth.contract(address=Web3.to_checksum_address(LINK_GRAPH_ADDRESS), abi=lg_abi)

    try:
        next_post_id = reg.functions.nextPostId().call()
    except Exception as e:
        logger.error("Could not read nextPostId: %s", e)
        return

    logger.info("Full sync: indexing posts 1..%d", next_post_id - 1)

    for pid in range(1, next_post_id):
        index_post(db, pid)

    # Index links
    for pid in range(1, next_post_id):
        try:
            outgoing = lg.functions.getOutgoing(pid).call()
            for edge in outgoing:
                to_id = edge[0]
                link_pid = edge[1]
                is_challenge = edge[2]
                index_link(db, link_pid, pid, to_id, is_challenge)
        except Exception:
            pass

    index_global_stats(db)
    logger.info("Full sync complete: %d posts indexed", next_post_id - 1)


# ── Event poller ────────────────────────────────────────

def _get_last_block(db: Session, contract_name: str) -> int:
    row = db.execute(sql_text(
        "SELECT value FROM chain_indexer_state WHERE key = :k"
    ), {"k": f"last_block_{contract_name}"}).fetchone()
    return int(row[0]) if row else 0


def _set_last_block(db: Session, contract_name: str, block: int):
    db.execute(sql_text("""
        INSERT INTO chain_indexer_state (key, value, updated_at)
        VALUES (:k, :v, now())
        ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = now()
    """), {"k": f"last_block_{contract_name}", "v": str(block)})
    db.commit()



def _reindex_connected(db: Session, post_id: int):
    """Re-index all posts connected to this post via links (one hop)."""
    try:
        rows = db.execute(sql_text(
            "SELECT DISTINCT from_post_id FROM chain_link WHERE to_post_id = :pid "
            "UNION "
            "SELECT DISTINCT to_post_id FROM chain_link WHERE from_post_id = :pid "
            "UNION "
            "SELECT DISTINCT link_post_id FROM chain_link WHERE from_post_id = :pid OR to_post_id = :pid"
        ), {"pid": post_id}).fetchall()
        for (connected_pid,) in rows:
            if connected_pid != post_id:
                index_post(db, connected_pid)
    except Exception as e:
        logger.debug("_reindex_connected(%d) failed: %s", post_id, e)

def poll_events(db: Session):
    """Poll for new events from all contracts and index affected posts."""
    w3 = _get_w3()
    current_block = w3.eth.block_number

    # StakeEngine events
    se_abi = _load_abi("StakeEngine")
    se = w3.eth.contract(address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS), abi=se_abi)
    last_block = _get_last_block(db, "StakeEngine")
    if last_block == 0:
        last_block = max(current_block - 10000, 0)

    from_block = last_block + 1
    to_block = min(from_block + BLOCK_BATCH, current_block)

    if from_block <= to_block:
        affected_posts = set()
        affected_users = {}  # post_id -> set of addresses

        try:
            # StakeAdded
            for event in se.events.StakeAdded.get_logs(from_block=from_block, to_block=to_block):
                pid = event.args.postId
                staker = event.args.staker.lower()
                affected_posts.add(pid)
                affected_users.setdefault(pid, set()).add(staker)

            # StakeWithdrawn
            for event in se.events.StakeWithdrawn.get_logs(from_block=from_block, to_block=to_block):
                pid = event.args.postId
                staker = event.args.staker.lower()
                affected_posts.add(pid)
                affected_users.setdefault(pid, set()).add(staker)

            # PostUpdated (snapshot)
            for event in se.events.PostUpdated.get_logs(from_block=from_block, to_block=to_block):
                affected_posts.add(event.args.postId)

        except Exception as e:
            logger.warning("Error polling StakeEngine events: %s", e)

        for pid in affected_posts:
            users = list(affected_users.get(pid, []))
            index_post(db, pid, user_addresses=users)

        _set_last_block(db, "StakeEngine", to_block)

    # PostRegistry events
    reg_abi = _load_abi("PostRegistry")
    reg = w3.eth.contract(address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS), abi=reg_abi)
    last_block = _get_last_block(db, "PostRegistry")
    if last_block == 0:
        last_block = max(current_block - 10000, 0)

    from_block = last_block + 1
    to_block = min(from_block + BLOCK_BATCH, current_block)

    if from_block <= to_block:
        try:
            for event in reg.events.PostCreated.get_logs(from_block=from_block, to_block=to_block):
                pid = event.args.postId
                creator = event.args.creator.lower()
                index_post(db, pid, user_addresses=[creator])
        except Exception as e:
            logger.warning("Error polling PostRegistry events: %s", e)
        _set_last_block(db, "PostRegistry", to_block)

    # LinkGraph events
    lg_abi = _load_abi("LinkGraph")
    lg = w3.eth.contract(address=Web3.to_checksum_address(LINK_GRAPH_ADDRESS), abi=lg_abi)
    last_block = _get_last_block(db, "LinkGraph")
    if last_block == 0:
        last_block = max(current_block - 10000, 0)

    from_block = last_block + 1
    to_block = min(from_block + BLOCK_BATCH, current_block)

    if from_block <= to_block:
        try:
            for event in lg.events.EdgeAdded.get_logs(from_block=from_block, to_block=to_block):
                from_pid = event.args["from"]
                to_pid = event.args["to"]
                link_pid = event.args.linkPostId
                is_challenge = event.args.isChallenge
                index_link(db, link_pid, from_pid, to_pid, is_challenge)
                # Re-index all connected posts (link affects VS of targets)
                index_post(db, to_pid)
                index_post(db, from_pid)
                index_post(db, link_pid)
                # Also re-index any posts linked to/from the affected claims
                _reindex_connected(db, to_pid)
                _reindex_connected(db, from_pid)
        except Exception as e:
            logger.warning("Error polling LinkGraph events: %s", e)
        _set_last_block(db, "LinkGraph", to_block)

    # Global stats (every poll)
    index_global_stats(db)


# ── Background thread ───────────────────────────────────

_indexer_thread = None

def start_indexer():
    """Start the background indexer thread."""
    global _indexer_thread
    if _indexer_thread is not None and _indexer_thread.is_alive():
        return

    def _run():
        logger.info("Chain indexer starting...")

        # Initial full sync
        try:
            db = get_session_factory()()
            full_sync(db)
            db.close()
        except Exception as e:
            logger.error("Full sync failed: %s", e)

        # Poll loop
        while True:
            try:
                db = get_session_factory()()
                poll_events(db)
                db.close()
            except Exception as e:
                logger.warning("Indexer poll error: %s", e)
            time.sleep(POLL_INTERVAL)

    _indexer_thread = threading.Thread(target=_run, daemon=True, name="chain-indexer")
    _indexer_thread.start()
    logger.info("Chain indexer thread started (poll every %ds)", POLL_INTERVAL)


def trigger_reindex(post_id: int, user_address: str | None = None):
    """Trigger immediate re-indexing of a post (called by relay after meta-tx)."""
    try:
        db = get_session_factory()()
        users = [user_address] if user_address else None
        index_post(db, post_id, user_addresses=users)
        db.close()
    except Exception as e:
        logger.warning("Trigger reindex failed for post %d: %s", post_id, e)
def _queue_article_refresh(db, post_id):
    """Trigger immediate background article cache rebuild for any topic
    affected by this post. Called by relay.py after stake/claim changes."""
    import threading, logging
    logger = logging.getLogger(__name__)
    try:
        from sqlalchemy import text as sql_text
        rows = db.execute(sql_text(
            "SELECT DISTINCT topic_key FROM article_sentence "
            "WHERE post_id = :pid"
        ), {"pid": post_id}).fetchall()

        for (topic_key,) in rows:
            def _rebuild(tk):
                try:
                    from db import get_session_factory
                    from articles.article_store import build_and_cache_response
                    build_and_cache_response(get_session_factory(), tk)
                except Exception as e:
                    logger.debug("Article cache rebuild failed for '%s': %s", tk, e)

            threading.Thread(
                target=_rebuild,
                args=(topic_key,),
                daemon=True,
                name=f"rebuild-{topic_key[:20]}",
            ).start()

        if rows:
            logger.info("Triggered cache rebuild for %d topics (post_id=%d)",
                        len(rows), post_id)
    except Exception as e:
        logger.warning("Article refresh trigger failed (non-fatal): %s", e)

