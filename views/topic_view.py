# app/views/topic_view.py
"""
View 1: Topic View — Incumbent vs Challenger layout.

Single LLM call handles dedup, challenger pairing, and irrelevant filtering.
"""

import json
import logging
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from semantic import compute_one

logger = logging.getLogger(__name__)

TOPIC_RELEVANCE_THRESHOLD = 0.65


def _fetch_on_chain_claims(db: Session, topic_embedding: str, limit: int = 20):
    """Fetch all on-chain claims related to this topic via embedding similarity."""
    try:
        rows = db.execute(
            sql_text("""
                SELECT c.claim_id, c.claim_text, c.post_id,
                       1 - (ce.embedding <=> CAST(:vec AS vector)) AS similarity
                FROM claim c
                JOIN claim_embedding ce ON ce.claim_id = c.claim_id
                WHERE c.post_id IS NOT NULL
                ORDER BY ce.embedding <=> CAST(:vec AS vector)
                LIMIT :lim
            """),
            {"vec": topic_embedding, "lim": limit},
        ).fetchall()
        result = [
            {
                "claim_id": r[0],
                "text": r[1],
                "post_id": r[2],
                "similarity": float(r[3]),
            }
            for r in rows
            if float(r[3]) >= TOPIC_RELEVANCE_THRESHOLD
        ]
        logger.info("Fetched %d on-chain claims (threshold %.2f)", len(result), TOPIC_RELEVANCE_THRESHOLD)
        for r in result:
            logger.info("  on-chain: sim=%.3f post_id=%s text=%s", r["similarity"], r["post_id"], r["text"][:80])
        return result
    except Exception as e:
        logger.warning("Failed to fetch on-chain claims: %s", e)
        return []


def _embed_text(text: str) -> str:
    from embedding import embed
    return embed(text)


def _classify_all(incumbent_texts, candidate_texts):
    """
    Single LLM call to classify ALL on-chain candidates.

    Returns dict: { "0": {"action":"merge","incumbent":2}, ... }
    """
    if not candidate_texts or not incumbent_texts:
        return {}

    try:
        from llm_provider import complete

        inc_json = json.dumps(
            [{"idx": i, "text": t} for i, t in enumerate(incumbent_texts)],
            indent=2,
        )
        cand_json = json.dumps(
            [{"idx": i, "text": t} for i, t in enumerate(candidate_texts)],
            indent=2,
        )

        system = (
            "You classify on-chain claims relative to AI-generated incumbent claims. "
            "Respond with ONLY a JSON object, no explanation or markdown."
        )

        prompt = (
            "INCUMBENTS (AI-generated mainstream claims):\n"
            + inc_json + "\n\n"
            "CANDIDATES (on-chain user-submitted claims to classify):\n"
            + cand_json + "\n\n"
            "For EACH candidate, decide ONE action:\n\n"
            '1. "merge" — Candidate says the SAME THING as an incumbent, just worded differently.\n'
            '   Return: {"action": "merge", "incumbent": N}\n'
            '   Example: "Earth is round and slightly flattened" MERGES with "Earth is an oblate spheroid"\n\n'
            '2. "challenge" — Candidate DIRECTLY CONTRADICTS an incumbent. They CANNOT BOTH BE TRUE.\n'
            '   Return: {"action": "challenge", "incumbent": N}\n'
            '   CRITICAL: Match to the incumbent about the SAME ASPECT/TOPIC.\n'
            '   "The Earth is flat" challenges the SHAPE claim (oblate spheroid), NOT the POSITION claim (third planet).\n'
            '   "The sun revolves around earth" challenges ORBITAL/HELIOCENTRIC claims, NOT naming/numbering claims.\n\n'
            '3. "extra" — On-topic but says something NEW not covered by any incumbent.\n'
            '   Return: {"action": "extra"}\n\n'
            '4. "irrelevant" — Off-topic, unrelated.\n'
            '   Return: {"action": "irrelevant"}\n\n'
            "Respond with ONLY a JSON object. Example:\n"
            '{"0": {"action": "challenge", "incumbent": 2}, "1": {"action": "merge", "incumbent": 0}, "2": {"action": "extra"}}\n'
        )

        logger.info("LLM classify: %d incumbents, %d candidates", len(incumbent_texts), len(candidate_texts))

        raw = complete(prompt, system=system, max_tokens=1000, temperature=0)
        logger.info("LLM raw response: %s", raw[:500])

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)
        logger.info("LLM parsed classification: %s", result)
        return result

    except Exception as e:
        logger.error("LLM classification failed: %s", e, exc_info=True)
        return {}


def _enrich_claim(db, claim, user_address=None):
    """Add on-chain state (stakes, VS) to a claim dict."""
    post_id = claim.get("post_id")
    claim.setdefault("stake_support", 0)
    claim.setdefault("stake_challenge", 0)
    claim.setdefault("verity_score", 0.0)
    claim.setdefault("user_support", 0)
    claim.setdefault("user_challenge", 0)

    if post_id is not None:
        try:
            from chain.chain_reader import get_stake_totals, get_user_stake, get_verity_score
            s, c = get_stake_totals(post_id)
            claim["stake_support"] = s
            claim["stake_challenge"] = c
            claim["verity_score"] = get_verity_score(post_id)
            if user_address:
                claim["user_support"] = get_user_stake(user_address, post_id, 0)
                claim["user_challenge"] = get_user_stake(user_address, post_id, 1)
        except Exception as e:
            logger.warning("Failed to enrich claim post_id=%s: %s", post_id, e)

    on_chain = compute_one(db, claim["text"], top_k=3)
    claim["on_chain"] = on_chain

    return claim


def build_topic_view(db, ai_claims, topic_text, user_address=None):
    """
    Build incumbent/challenger rows for View 1.

    Returns list of: { incumbent: ClaimDict, challengers: [ClaimDict...] }
    """
    logger.info("build_topic_view: %d AI claims, topic=%s", len(ai_claims), topic_text[:80])

    topic_vec = _embed_text(topic_text)
    on_chain_claims = _fetch_on_chain_claims(db, topic_vec, limit=30)

    # Phase 1: Exact hash matches for AI claims
    hash_matched_ids = set()
    ai_hash_map = {}
    for i, ai_claim in enumerate(ai_claims):
        on_chain = compute_one(db, ai_claim.get("text", ""), top_k=1)
        if on_chain.get("post_id") is not None:
            ai_hash_map[i] = on_chain
            for oc in on_chain_claims:
                if oc["post_id"] == on_chain["post_id"]:
                    hash_matched_ids.add(oc["claim_id"])
                    break

    logger.info("Hash matched %d AI claims to on-chain", len(ai_hash_map))

    # Phase 2: Candidates = on-chain claims NOT already hash-matched
    candidates = [oc for oc in on_chain_claims if oc["claim_id"] not in hash_matched_ids]
    logger.info("Candidates for LLM classification: %d", len(candidates))

    # Phase 3: LLM classification
    incumbent_texts = [ai.get("text", "") for ai in ai_claims]
    candidate_texts = [c["text"] for c in candidates]
    classification = _classify_all(incumbent_texts, candidate_texts) if candidates else {}

    # Fallback: if LLM classification is empty but we have candidates,
    # use VS-based heuristic
    if not classification and candidates:
        logger.warning("LLM classification empty — using VS-based fallback for %d candidates", len(candidates))
        classification = {}
        for ci, c in enumerate(candidates):
            try:
                from chain.chain_reader import get_stake_totals, get_verity_score
                s, ch = get_stake_totals(c["post_id"])
                vs = get_verity_score(c["post_id"])
            except Exception:
                vs = 0
            if vs < 0:
                best_inc = 0
                best_overlap = 0
                cand_words = set(c["text"].lower().split())
                for ii, itxt in enumerate(incumbent_texts):
                    inc_words = set(itxt.lower().split())
                    overlap = len(cand_words & inc_words)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_inc = ii
                classification[str(ci)] = {"action": "challenge", "incumbent": best_inc}
                logger.info("Fallback CHALLENGE: cand[%d] '%s' (VS=%s) -> inc[%d]", ci, c["text"][:40], vs, best_inc)
            else:
                classification[str(ci)] = {"action": "extra"}
                logger.info("Fallback EXTRA: cand[%d] '%s' (VS=%s)", ci, c["text"][:40], vs)

    # Phase 4: Build incumbents
    incumbents = []
    for i, ai_claim in enumerate(ai_claims):
        claim = {
            "text": ai_claim.get("text", ""),
            "post_id": None,
            "confidence": ai_claim.get("confidence", 0),
            "source": "ai",
            "author": ai_claim.get("author", "AI Search"),
        }
        if i in ai_hash_map:
            claim["post_id"] = ai_hash_map[i]["post_id"]
            claim["source"] = "ai+chain"
        incumbents.append(claim)

    # Phase 5: Apply merges from LLM classification
    merged_cand_indices = set()
    for cand_idx_str, cls in classification.items():
        try:
            cand_idx = int(cand_idx_str)
        except (ValueError, TypeError):
            continue
        if cand_idx >= len(candidates):
            continue

        action = None
        inc_idx = None
        if isinstance(cls, dict):
            action = cls.get("action")
            inc_idx = cls.get("incumbent")
        elif isinstance(cls, str):
            action = cls

        if action == "merge" and isinstance(inc_idx, int) and inc_idx < len(incumbents):
            if incumbents[inc_idx]["post_id"] is None:
                oc = candidates[cand_idx]
                incumbents[inc_idx]["text"] = oc["text"]
                incumbents[inc_idx]["post_id"] = oc["post_id"]
                incumbents[inc_idx]["source"] = "ai+chain"
                merged_cand_indices.add(cand_idx)
                logger.info("MERGE: cand[%d] '%s' -> incumbent[%d]", cand_idx, oc["text"][:50], inc_idx)

    # Enrich all incumbents
    for i in range(len(incumbents)):
        incumbents[i] = _enrich_claim(db, incumbents[i], user_address)

    # Phase 6: Build enriched candidates (excluding merged)
    enriched_candidates = []
    cand_to_enriched = {}
    for ci, c in enumerate(candidates):
        if ci in merged_cand_indices:
            continue
        claim = {
            "text": c["text"],
            "post_id": c["post_id"],
            "source": "chain",
            "author": "On-Chain",
            "topic_similarity": c["similarity"],
        }
        claim = _enrich_claim(db, claim, user_address)
        eidx = len(enriched_candidates)
        cand_to_enriched[ci] = eidx
        enriched_candidates.append(claim)

    # Phase 7: Assign challengers vs extras
    challenger_map = {}
    extras = []

    for cand_idx_str, cls in classification.items():
        try:
            cand_idx = int(cand_idx_str)
        except (ValueError, TypeError):
            continue
        if cand_idx in merged_cand_indices or cand_idx not in cand_to_enriched:
            continue

        cand = enriched_candidates[cand_to_enriched[cand_idx]]

        action = None
        inc_idx = None
        if isinstance(cls, dict):
            action = cls.get("action")
            inc_idx = cls.get("incumbent")
        elif isinstance(cls, str):
            action = cls

        if action == "challenge" and isinstance(inc_idx, int) and inc_idx < len(incumbents):
            challenger_map.setdefault(inc_idx, []).append(cand)
            logger.info("CHALLENGE: cand[%d] '%s' -> incumbent[%d] '%s'",
                        cand_idx, cand["text"][:40], inc_idx, incumbents[inc_idx]["text"][:40])
        elif action == "extra":
            extras.append(cand)
            logger.info("EXTRA: cand[%d] '%s'", cand_idx, cand["text"][:50])
        elif action == "irrelevant":
            logger.info("IRRELEVANT: cand[%d] '%s'", cand_idx, cand["text"][:50])
        else:
            extras.append(cand)
            logger.info("UNKNOWN action '%s': cand[%d] '%s' -> treating as extra", action, cand_idx, cand["text"][:50])

    # Catch unclassified candidates
    classified_indices = set()
    for k in classification.keys():
        try:
            classified_indices.add(int(k))
        except (ValueError, TypeError):
            pass
    for ci in range(len(candidates)):
        if ci in merged_cand_indices or ci in classified_indices:
            continue
        if ci in cand_to_enriched:
            cand = enriched_candidates[cand_to_enriched[ci]]
            if cand.get("topic_similarity", 0) >= 0.75:
                extras.append(cand)
                logger.info("UNCLASSIFIED (included as extra): cand[%d] '%s' sim=%.3f",
                            ci, cand["text"][:50], cand.get("topic_similarity", 0))

    # Phase 8: Build rows
    rows = []
    for i, inc in enumerate(incumbents):
        rows.append({"incumbent": inc, "challengers": challenger_map.get(i, [])})

    for cand in extras:
        rows.append({"incumbent": cand, "challengers": []})

    logger.info("Built %d rows (%d with challengers)", len(rows), sum(1 for r in rows if r["challengers"]))

    return rows