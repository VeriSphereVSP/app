

@router.get("/all")
def all_claims(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """List all on-chain claims with metrics."""
    rows = db.execute(sql_text("""
        SELECT c.claim_id, c.claim_text, c.post_id, c.created_tms,
               COALESCE(ta.topic_key, '') as topic
        FROM claim c
        LEFT JOIN article_sentence s ON s.post_id = c.post_id
        LEFT JOIN article_section sec ON s.section_id = sec.section_id
        LEFT JOIN topic_article ta ON sec.article_id = ta.article_id
        WHERE c.post_id IS NOT NULL
        GROUP BY c.claim_id, c.claim_text, c.post_id, c.created_tms, ta.topic_key
        ORDER BY c.post_id
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset}).fetchall()

    if not rows:
        return {"claims": [], "total": 0}

    views = _views()
    results = []

    for row in rows:
        post_id = row[2]
        if post_id is None:
            continue
        try:
            s = views.functions.getClaimSummary(post_id).call()
            support = _wei_to_vsp(int(s[1]))
            challenge = _wei_to_vsp(int(s[2]))
            total = _wei_to_vsp(int(s[3]))
            vs = _ray_to_pct(int(s[7]))
            base_vs = _ray_to_pct(int(s[6]))
            incoming = int(s[8])
            outgoing = int(s[9])
            controversy = total * (100 - abs(vs)) / 100 if total > 0 else 0

            results.append({
                "post_id": post_id,
                "text": str(s[0]),
                "verity_score": vs,
                "base_vs": base_vs,
                "stake_support": round(support, 4),
                "stake_challenge": round(challenge, 4),
                "total_stake": round(total, 4),
                "controversy": round(controversy, 4),
                "incoming_links": incoming,
                "outgoing_links": outgoing,
                "topic": row[4] or "",
                "created_at": row[3].isoformat() if row[3] else None,
            })
        except Exception as e:
            logger.warning(f"Failed to fetch summary for post {post_id}: {e}")
            continue

    results.sort(key=lambda x: -x["total_stake"])
    total_count = db.execute(sql_text(
        "SELECT COUNT(*) FROM claim WHERE post_id IS NOT NULL"
    )).scalar() or 0

    return {"claims": results, "total": total_count}
