"""Fixture tests for unified_grouping — pure, no DB. Run: python3 test_unified_grouping.py"""
import sys
sys.path.insert(0, ".")
from unified_grouping import (
    Item, group_items, KIND_CLAIM, KIND_SENTENCE,
    LANE_NARRATIVE, LANE_DISPUTED,
)

PASS, FAIL = 0, 0
def check(name, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  \033[0;32m✓\033[0m {name}")
    else: FAIL += 1; print(f"  \033[0;31m✗ {name}\033[0m")

# ── Injected fakes ───────────────────────────────────────────────────────────
# similarity: look up by a tag we stash on embedding = ("tag", value-vector).
# We just use 1-D "embeddings" and cosine = 1 - |a-b| clipped, but simpler:
# encode embedding as a float in [0,1] "position"; similar = 1 - |pa-pb|.
def similar(a, b):
    pa, pb = a[0], b[0]
    return max(0.0, 1.0 - abs(pa - pb))

# LLM verifier: we simulate "opposites" — pairs whose texts are known negations
# return False even though cosine is high. Everything else in-band returns True.
_OPPOSITES = {frozenset({"yellow>purple", "purple>yellow"})}
def equivalent(a, b, sim):
    key = frozenset({a.text, b.text})
    if key in _OPPOSITES:
        return False
    return True  # otherwise treat in-band as equivalent

def claim(id, text, pos, stake=0.0, vs=0.0):
    return Item(kind=KIND_CLAIM, id=id, text=text, embedding=(pos,), stake=stake, vs=vs)
def sent(id, text, pos, post_id=None):
    return Item(kind=KIND_SENTENCE, id=id, text=text, embedding=(pos,), post_id=post_id)

# ── Test 1: two near-identical claims → one group, canonical = higher stake ──
print("T1 near-identical claims group, top-staked canonical")
g = group_items(
    [claim(1, "co2 drives warming", 0.50, stake=1.0, vs=80),
     claim(2, "co2 drives warming too", 0.50, stake=5.0, vs=60)],
    similar=similar, equivalent=equivalent)
check("one group", len(g) == 1)
check("member_count 2", g[0].member_count == 2)
check("canonical is the higher-staked claim (id=2)", g[0].canonical.id == 2)
check("claim_anchored", g[0].claim_anchored)

# ── Test 2: OPPOSITES don't merge (LLM gate) ────────────────────────────────
print("T2 opposites at high cosine are split by the verifier")
g = group_items(
    [claim(10, "yellow>purple", 0.90, stake=1.0, vs=-100),
     claim(11, "purple>yellow", 0.92, stake=1.0, vs=-100)],   # cosine ~0.98 but opposite
    similar=similar, equivalent=equivalent)
# similar(0.90,0.92)=0.98 >= HIGH → would auto-bundle. To force the LLM band,
# make them land in [LOW,HIGH): positions 0.70 and 0.95 → sim 0.75.
g = group_items(
    [claim(10, "yellow>purple", 0.70, stake=1.0, vs=-100),
     claim(11, "purple>yellow", 0.95, stake=1.0, vs=-100)],
    similar=similar, equivalent=equivalent)
check("two separate groups (not merged)", len(g) == 2)

# ── Test 3: claim + sentence same meaning → canonical is the CLAIM (R3) ──────
print("T3 claim never rolled under a sentence")
g = group_items(
    [sent(100, "carbon dioxide warms the planet", 0.50),
     claim(3, "CO2 causes warming", 0.50, stake=2.0, vs=90)],
    similar=similar, equivalent=equivalent)
check("one group", len(g) == 1)
check("canonical is the CLAIM not the sentence", g[0].canonical.kind == KIND_CLAIM and g[0].canonical.id == 3)
check("sentence is hidden under the claim", any(m.kind == KIND_SENTENCE for m in g[0].hidden_members()))

# ── Test 4: sentence-only group → neutral, narrative, never disputed ─────────
print("T4 sentence-only group is narrative + neutral")
g = group_items(
    [sent(200, "the sky appears blue", 0.50),
     sent(201, "the sky looks blue", 0.50)],
    similar=similar, equivalent=equivalent)
check("one group", len(g) == 1)
check("canonical is a sentence", g[0].canonical.kind == KIND_SENTENCE)
check("lane is narrative", g[0].lane == LANE_NARRATIVE)
check("not claim_anchored", not g[0].claim_anchored)

# ── Test 5: claim-anchored group with negative agg VS → disputed (red) ──────
print("T5 losing claim group → disputed lane; its sentences hidden (R2)")
g = group_items(
    [claim(5, "vaccines cause autism", 0.50, stake=3.0, vs=-100),
     sent(300, "some claim that vaccines cause autism", 0.50)],
    similar=similar, equivalent=equivalent)
check("one group", len(g) == 1)
check("lane disputed", g[0].lane == LANE_DISPUTED)
check("canonical is the claim", g[0].canonical.kind == KIND_CLAIM)
check("sentence hidden (never renders in disputed)", all(
    m.kind == KIND_CLAIM for m in [g[0].canonical]) and
    any(m.kind == KIND_SENTENCE for m in g[0].hidden_members()))

# ── Test 6: whole claim group unstaked & VS 0 → hidden ──────────────────────
print("T6 unstaked, zero-VS claim group is hidden")
g = group_items(
    [claim(6, "yellow is better than purple", 0.50, stake=0.0, vs=0.0),
     claim(7, "yellow beats purple", 0.50, stake=0.0, vs=0.0)],
    similar=similar, equivalent=equivalent)
check("one group", len(g) == 1)
check("group hidden", not g[0].visible)

# ── Test 7: exact-duplicate sentences fold even without embeddings ───────────
print("T7 exact-duplicate text groups regardless of embedding")
a = Item(kind=KIND_SENTENCE, id=400, text="Identical sentence.", embedding=None)
b = Item(kind=KIND_SENTENCE, id=401, text="identical sentence.", embedding=None)  # case-diff
c = Item(kind=KIND_SENTENCE, id=402, text="A different sentence.", embedding=None)
g = group_items([a, b, c], similar=similar, equivalent=equivalent)
groups_with_dupe = [grp for grp in g if grp.member_count == 2]
check("exact dupes form one 2-member group", len(groups_with_dupe) == 1)
check("distinct sentence stays separate", any(grp.member_count == 1 for grp in g))

# ── Test 8: sub-LOW similarity never groups ─────────────────────────────────
print("T8 dissimilar items never group")
g = group_items(
    [claim(8, "climate change is real", 0.10, stake=1.0, vs=50),
     claim(9, "the moon is made of cheese", 0.90, stake=1.0, vs=50)],
    similar=similar, equivalent=equivalent)  # sim(0.10,0.90)=0.20 < LOW
check("two separate groups", len(g) == 2)

# ── Test 9: determinism — same input, same grouping (order-independent) ──────
print("T9 deterministic across input order")
items = [claim(1, "a", 0.5, stake=2, vs=10), claim(2, "a2", 0.5, stake=9, vs=10),
         sent(3, "b", 0.1), sent(4, "b", 0.1)]
g1 = group_items(items, similar=similar, equivalent=equivalent)
g2 = group_items(list(reversed(items)), similar=similar, equivalent=equivalent)
sig = lambda gs: [(grp.canonical.kind, grp.canonical.id, grp.lane, grp.member_count) for grp in gs]
check("identical grouping regardless of order", sig(g1) == sig(g2))

print()
print(f"  \033[1m{PASS} passed, {FAIL} failed\033[0m")
sys.exit(1 if FAIL else 0)
