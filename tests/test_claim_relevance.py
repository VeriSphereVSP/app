"""Tests for the article relevance gate in articles/claim_indexer.py.

These tests exercise is_claim_relevant_to_article with a lightweight
fake DB and a mocked embed() so the regression that motivated the fix
— a sex-change claim being injected into the Climate Change article
because of crude stem overlap on the word "change" — cannot recur
silently.

Pure unit tests; no DB connection required. Run with:
    pytest tests/test_claim_relevance.py -v
"""
import math
import pytest


# ────────────────────────────────────────────────────────────────────
# Fake DB — implements just the SQLAlchemy-Session surface that
# is_claim_relevant_to_article and its helpers actually use.
# ────────────────────────────────────────────────────────────────────

class _FakeRow:
    """Tuple-like row that also supports attribute access by index."""
    def __init__(self, values):
        self._values = tuple(values)

    def __getitem__(self, i):
        return self._values[i]

    def __iter__(self):
        return iter(self._values)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class FakeDB:
    """Minimal fake of an SQLAlchemy Session for the relevance gate.

    Articles and their sections are inserted via .seed_article(); the
    .execute() method does crude pattern-matching on the SQL text to
    return the right rows. This is fragile by design — if we ever
    change the queries in is_claim_relevant_to_article, the tests will
    fail loudly here, which is the point.
    """
    def __init__(self):
        self._articles = {}   # article_id -> {title, topic_key, sections: [(heading, [sentences])]}
        self._next_aid = 1

    def seed_article(self, topic_key, title, sections):
        aid = self._next_aid
        self._next_aid += 1
        self._articles[aid] = {
            "title": title,
            "topic_key": topic_key,
            "sections": sections,
        }
        return aid

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}

        if "FROM topic_article WHERE article_id" in sql and "title" in sql:
            a = self._articles.get(params["a"])
            if not a:
                return _FakeResult([])
            return _FakeResult([_FakeRow((a["title"], a["topic_key"]))])

        if "FROM article_section sec WHERE sec.article_id" in sql:
            a = self._articles.get(params["a"])
            if not a:
                return _FakeResult([])
            return _FakeResult([
                _FakeRow((i + 1, sec[0])) for i, sec in enumerate(a["sections"])
            ])

        if "FROM article_sentence" in sql and "section_id = :s" in sql:
            # First sentence of section. Section IDs are 1-indexed
            # within an article; we match by position across all
            # articles' sections, which is good enough for tests with
            # one article at a time.
            sid = params["s"]
            for a in self._articles.values():
                if 1 <= sid <= len(a["sections"]):
                    sentences = a["sections"][sid - 1][1]
                    if sentences:
                        return _FakeResult([_FakeRow((sentences[0],))])
                    return _FakeResult([])
            return _FakeResult([])

        raise NotImplementedError(f"FakeDB does not handle SQL: {sql[:120]}")


# ────────────────────────────────────────────────────────────────────
# Mocked embed + cosine_similarity that score by topic family.
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_embed(monkeypatch):
    """Replace embedding.embed and similarity.cosine_similarity with
    deterministic fakes that score by topic family.

    Each text is mapped to a "topic family" via a keyword table; we
    encode the family as a one-hot 8-dim vector. Cosine similarity is
    1.0 within a family and ~0.0 across families. This lets us test
    the relevance gate without an actual embedding model.
    """
    families = {
        "climate":  {"climate", "weather", "atmosphere", "greenhouse",
                     "milankovitch", "glacial", "interglacial", "warming",
                     "fossil fuel", "co2", "carbon dioxide", "ipcc"},
        "biology":  {"sex", "medical", "surgical", "human being",
                     "procedure", "anatomy", "chromosome"},
        "politics": {"election", "government", "policy", "senate"},
    }

    def family_of(text: str):
        t = (text or "").lower()
        scores = {}
        for fam, keys in families.items():
            scores[fam] = sum(1 for k in keys if k in t)
        if not scores:
            return None
        best = max(scores, key=lambda f: scores[f])
        return best if scores[best] > 0 else None

    def fake_embed_fn(text: str):
        fam = family_of(text)
        slot = {"climate": 0, "biology": 1, "politics": 2}.get(fam, 7)
        v = [0.05] * 8
        v[slot] = 1.0
        return v

    def fake_cosine(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    import embedding
    import similarity
    monkeypatch.setattr(embedding, "embed", fake_embed_fn)
    monkeypatch.setattr(similarity, "cosine_similarity", fake_cosine)
    return fake_embed_fn


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────

def test_climate_article_rejects_sex_change_claim(fake_embed):
    """The original bug: a sex-change claim must NOT be considered
    relevant to a Climate Change article, even though both contain
    the word "change"."""
    from articles.claim_indexer import is_claim_relevant_to_article

    db = FakeDB()
    article_id = db.seed_article(
        "climate change",
        "Climate Change",
        [
            ("Definition and Overview", [
                "Climate change refers to long-term shifts in global temperatures and weather patterns.",
            ]),
            ("Causes", [
                "The burning of fossil fuels is the largest source of greenhouse gas emissions.",
            ]),
        ],
    )

    bad_claim = (
        "There is no medical or surgical procedure or process that can "
        "change the sex of a human being."
    )
    assert is_claim_relevant_to_article(db, article_id, bad_claim) is False


def test_climate_article_accepts_milankovitch_claim(fake_embed):
    """An on-thesis but minority-view claim about Milankovitch cycles
    SHOULD be admitted into the Climate Change article. The protocol's
    job is to admit dissenting on-chain content, not editorialize."""
    from articles.claim_indexer import is_claim_relevant_to_article

    db = FakeDB()
    article_id = db.seed_article(
        "climate change",
        "Climate Change",
        [
            ("Definition and Overview", [
                "Climate change refers to long-term shifts in global temperatures and weather patterns.",
            ]),
            ("Causes", [
                "The burning of fossil fuels is the largest source of greenhouse gas emissions.",
            ]),
        ],
    )

    on_thesis_claim = (
        "Milankovitch cycles are the primary driver of all climate change, "
        "meaning glacial and interglacial periods, for at least the past 2.6 "
        "million years of the Quaternary Period."
    )
    assert is_claim_relevant_to_article(db, article_id, on_thesis_claim) is True


def test_substring_fast_path_still_accepts_topic_mention(fake_embed):
    """If a claim text literally contains the topic_key, accept fast
    without needing the embedding model."""
    from articles.claim_indexer import is_claim_relevant_to_article

    db = FakeDB()
    article_id = db.seed_article(
        "climate change", "Climate Change",
        [("Overview", ["Climate change is real."])],
    )
    assert is_claim_relevant_to_article(
        db, article_id,
        "Anthropogenic climate change is well established.",
    ) is True


def test_short_topic_does_not_match_substring_fast_path(fake_embed):
    """If topic_key is shorter than 4 chars, the substring fast-path
    must NOT trigger. Topics like 'AI' would otherwise match every
    claim containing 'air', 'paint', 'mountain', etc."""
    from articles.claim_indexer import is_claim_relevant_to_article

    db = FakeDB()
    article_id = db.seed_article(
        "ai", "AI",
        [("Overview", ["Artificial intelligence is a field of computer science."])],
    )
    # A claim about elections — different family in the fake embedder.
    # With substring disabled for short topic_keys, this should fall
    # through to the embedding check and be rejected.
    assert is_claim_relevant_to_article(
        db, article_id,
        "The senate passed a new election bill.",
    ) is False


def test_embedding_failure_rejects_conservatively(monkeypatch):
    """If the embedding model fails outright, the relevance gate
    must reject the claim rather than fall back to stem overlap (the
    historical buggy fallback). Better to miss a relevant claim than
    to pollute an article with cross-domain content."""
    from articles.claim_indexer import is_claim_relevant_to_article

    def broken_embed(text: str):
        raise RuntimeError("simulated embedding outage")

    import embedding
    monkeypatch.setattr(embedding, "embed", broken_embed)

    db = FakeDB()
    article_id = db.seed_article(
        "climate change", "Climate Change",
        [("Overview", ["Climate change is a long-term process."])],
    )
    # No substring match (no 'climate change' in this claim) and
    # embedding broken — must reject.
    assert is_claim_relevant_to_article(
        db, article_id,
        "There is no medical procedure to alter chromosomes.",
    ) is False
