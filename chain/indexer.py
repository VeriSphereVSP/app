# app/chain/indexer.py
"""
Background indexer for PostRegistry.
Polls nextPostId() and reads new claims via getPost/getClaim view calls.
Does NOT use eth_getLogs (incompatible with Alchemy free tier).
Catches claims created by external apps/wallets.
Our own relay writes update the DB immediately in relay.py.
"""

import asyncio
import logging
import re
import unicodedata
from web3 import Web3
from sqlalchemy import text as sql_text

from config import POST_REGISTRY_ADDRESS, RPC_URL
from db import get_session_factory

logger = logging.getLogger(__name__)

POST_REGISTRY_ABI = [
    {
        "type": "function",
        "name": "nextPostId",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getPost",
        "inputs": [{"name": "postId", "type": "uint256"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "creator", "type": "address"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "contentType", "type": "uint8"},
                    {"name": "contentId", "type": "uint256"},
                    {"name": "creationFee", "type": "uint256"},
                ],
            }
        ],
        "stateMutability": "view",
    },
    {
        "type": "function",
        "name": "getClaim",
        "inputs": [{"name": "claimId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
    },
]

CONTENT_TYPE_CLAIM = 0
POLL_INTERVAL = 12


def normalize_claim_text(text):
    text = unicodedata.normalize("NFC", text)
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _ensure_indexer_state_table(db):
    db.execute(sql_text(
        "CREATE TABLE IF NOT EXISTS indexer_state "
        "(key TEXT PRIMARY KEY, value BIGINT NOT NULL DEFAULT 0)"
    ))
    db.execute(sql_text(
        "INSERT INTO indexer_state (key, value) VALUES ('last_post_id', 0) "
        "ON CONFLICT (key) DO NOTHING"
    ))
    db.commit()


def _ensure_post_id_column(db):
    try:
        db.execute(sql_text(
            "ALTER TABLE claim ADD COLUMN post_id INTEGER DEFAULT NULL"
        ))
        db.commit()
        logger.info("Added post_id column to claim table")
    except Exception:
        db.rollback()


def _get_last_post_id(db):
    row = db.execute(sql_text(
        "SELECT value FROM indexer_state WHERE key = 'last_post_id'"
    )).fetchone()
    return int(row[0]) if row else 0


def _set_last_post_id(db, post_id):
    db.execute(sql_text(
        "UPDATE indexer_state SET value = :v WHERE key = 'last_post_id'"
    ), {"v": post_id})
    db.commit()


def _upsert_claim(db, claim_text, post_id):
    from hashing import content_hash
    from embedding import embed
    from config import EMBEDDINGS_MODEL

    normalized = normalize_claim_text(claim_text)
    h_norm = content_hash(normalized)
    h_orig = content_hash(claim_text)

    for h in [h_norm, h_orig]:
        row = db.execute(sql_text(
            "SELECT claim_id, post_id FROM claim WHERE content_hash = :h"
        ), {"h": h}).fetchone()
        if row:
            if row[1] is None:
                db.execute(sql_text(
                    "UPDATE claim SET post_id = :pid WHERE claim_id = :cid"
                ), {"pid": post_id, "cid": row[0]})
                logger.info("Updated claim (id=%d) with post_id=%d", row[0], post_id)
            db.commit()
            return

    result = db.execute(sql_text(
        "INSERT INTO claim (claim_text, content_hash, post_id) "
        "VALUES (:t, :h, :pid) RETURNING claim_id"
    ), {"t": claim_text, "h": h_orig, "pid": post_id}).fetchone()
    cid = int(result[0])

    try:
        vec = embed(claim_text)
        db.execute(sql_text(
            "INSERT INTO claim_embedding (claim_id, embedding_model, embedding) "
            "VALUES (:id, :m, :v)"
        ), {"id": cid, "m": EMBEDDINGS_MODEL, "v": vec})
    except Exception as e:
        logger.warning("Failed to embed claim %d: %s", cid, e)

    db.commit()
    logger.info("Indexed new on-chain claim: id=%d post_id=%d text=%s", cid, post_id, claim_text[:60])


def _sync_posts(w3, contract, db):
    try:
        next_post_id = contract.functions.nextPostId().call()
    except Exception as e:
        logger.warning("Failed to read nextPostId: %s", e)
        return

    last_synced = _get_last_post_id(db)
    if next_post_id <= last_synced:
        return

    new_count = 0
    for pid in range(last_synced, next_post_id):
        try:
            post = contract.functions.getPost(pid).call()
            content_type = post[2]
            content_id = post[3]

            if content_type != CONTENT_TYPE_CLAIM:
                continue

            claim_text = contract.functions.getClaim(content_id).call()
            if claim_text:
                _upsert_claim(db, claim_text, pid)
                new_count += 1
        except Exception as e:
            logger.warning("Failed to read post %d: %s", pid, e)

    _set_last_post_id(db, next_post_id)
    if new_count > 0:
        logger.info("Synced %d new claims (post_id %d..%d)", new_count, last_synced, next_post_id - 1)


async def run_indexer():
    if not POST_REGISTRY_ADDRESS or not RPC_URL:
        logger.warning("Indexer disabled: POST_REGISTRY or RPC_URL not configured")
        return

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=POST_REGISTRY_ABI,
    )

    SessionLocal = get_session_factory()
    db = SessionLocal()

    try:
        _ensure_indexer_state_table(db)
        _ensure_post_id_column(db)
        logger.info("Indexer running (polling nextPostId every %ds)", POLL_INTERVAL)
        _sync_posts(w3, contract, db)

        while True:
            try:
                _sync_posts(w3, contract, db)
            except Exception as e:
                logger.exception("Indexer error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)
    finally:
        db.close()