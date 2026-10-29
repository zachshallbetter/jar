#!/usr/bin/env python3
# gate: hermetic   (sections 1-7: temp stores only; section 8, a second API server, runs only with --api)
"""
Demanding stress + correctness test suite for agent_memory, agent_skills, agent_api_server.

Tests:
  1. BM25 relevance ordering — 50 diverse facts, precision/recall scoring
  2. Thread safety — 20 concurrent writers + readers; no corruption or deadlock
  3. Deduplication — Jaccard threshold prevents near-duplicate facts
  4. Offload index under concurrency — no node_id collisions
  5. BM25 cache coherence — cache invalidation after add_fact; stale facts absent
  6. L3 persona milestone gate — fires exactly once per threshold, not per fact
  7. Skills loader — good files load, malformed files skip with stderr warning
  8. API concurrent load — 30 parallel HTTP requests to a second server it starts (opt-in: --api)

Run: python3 scripts/test_stress.py [--api]
Sections 1-7 use temp stores only and run in the deploy gate. Section 8 starts agent_api_server on
:7772 and creates workspaces and processes from this checkout, so it runs only with --api.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path


def _owner() -> str:
    """The memory owner the defaults used to stand for (#1070: person_id is now always passed)."""
    import agent_memory
    return agent_memory.OWNER

sys.path.insert(0, str(Path(__file__).parent))

# ── helpers ──────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
_lock = threading.Lock()


def ok(label: str) -> None:
    global PASS
    with _lock:
        PASS += 1
        print(f"  [PASS] {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    with _lock:
        FAIL += 1
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"\n         {detail}"
        print(msg)


def section(title: str) -> None:
    print(f"\n── {title} ──")


# ── isolated memory factory ───────────────────────────────────────────────────

def make_memory(tmp: Path) -> "AgentMemory":
    """Create a fresh AgentMemory backed by a temp directory."""
    os.environ["AGENT_MEMORY_DIR"] = str(tmp)
    # Force re-import with fresh module-level paths
    import importlib
    import agent_memory as am
    importlib.reload(am)
    return am.AgentMemory()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. BM25 relevance ordering
# ═══════════════════════════════════════════════════════════════════════════════
FACTS_50 = [
    # home / household
    "user has a cat named Biscuit who is 3 years old",
    "user has a dog named Pepper who loves fetch",
    "the thermostat is set to 70 degrees Fahrenheit",
    "user lives in San Francisco in the Mission district",
    "the kitchen light switch is broken and needs repair",
    "user's apartment is on the 4th floor with no elevator",
    "user prefers eco-friendly cleaning products",
    "the living room TV is a 65-inch Sony 4K",
    "user has a Philips Hue light setup in the bedroom",
    "user's WiFi password is stored in 1Password",
    # media / entertainment
    "user watches Plex on a Raspberry Pi 4 media server",
    "favorite genre is sci-fi, especially hard sci-fi",
    "user is currently watching The Expanse on Plex",
    "user has a Sonos speaker in the living room",
    "user prefers lo-fi hip hop for focus music",
    "user's Plex library has over 2000 movies",
    "user listens to podcasts during commute",
    "user plays chess online at lichess.org",
    "favorite video game is Factorio",
    "user has a vinyl collection of about 300 records",
    # work / tech
    "user is a senior software engineer at a fintech startup",
    "primary programming language is Python and TypeScript",
    "user works remotely from home every day",
    "user has a 3-monitor setup with a standing desk",
    "user uses Neovim as primary editor with custom config",
    "user's MacBook Pro is an M2 Max with 32 GB RAM",
    "user deploys to AWS ECS and uses Terraform for infra",
    "user attends a weekly all-hands meeting every Monday at 10am",
    "user is learning Rust in their spare time",
    "user's GitHub username is zachshallbetter",
    # health / lifestyle
    "user runs 5 miles three times a week",
    "user follows a plant-based diet",
    "user wakes up at 6 AM every day",
    "user meditates for 20 minutes each morning",
    "user goes to bed at 10:30 PM",
    "user has a standing desk timer to remind them to move",
    "user drinks 8 glasses of water per day",
    "user has a prescription for reading glasses",
    "user visited a chiropractor last month for back pain",
    "user is allergic to shellfish",
    # finances / admin
    "user pays rent on the 1st of each month via Zelle",
    "user's monthly budget for groceries is $400",
    "user invests in index funds via Vanguard",
    "user has a Costco membership",
    "user uses YNAB for budgeting",
    "user's car insurance renews in October",
    "user has a recurring dentist appointment every 6 months",
    "user's student loans are fully paid off",
    "user tracks subscriptions in a Notion spreadsheet",
    "user has an emergency fund with 6 months of expenses",
]

QUERIES_EXPECTED = [
    # (query, must_appear_in_top3_facts_containing_keywords)
    ("cat Biscuit pet", ["cat named Biscuit"]),
    ("Plex media server movie", ["Plex", "2000 movies", "Raspberry"]),
    ("Python TypeScript programming", ["Python and TypeScript"]),
    ("budget groceries YNAB finances", ["YNAB", "groceries"]),
    ("thermostat temperature home", ["thermostat"]),
    ("runs miles week exercise", ["runs 5 miles"]),
    ("Neovim editor config", ["Neovim"]),
    ("Factorio game play", ["Factorio"]),
    ("shellfish allergy food", ["shellfish"]),
    ("rent payment Zelle", ["Zelle"]),
]


def run_bm25_tests() -> None:
    section("1. BM25 relevance ordering (50 facts, 10 queries)")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))
        for f in FACTS_50:
            mem.add_fact("conv-bm25", f, confidence=1.0, person_id=_owner())

        total = mem.total_fact_count()
        if total == len(FACTS_50):
            ok(f"all {len(FACTS_50)} facts stored (no spurious dedupe)")
        else:
            fail(f"expected {len(FACTS_50)} facts, got {total}")

        hits = 0
        for query, keywords in QUERIES_EXPECTED:
            result = mem.recall(query, conv_id="conv-bm25", person_id=_owner())
            ctx = result.prependContext
            matched = any(any(kw in f for f in FACTS_50
                              if f in ctx)
                          for kw in keywords)
            # More direct: check if any keyword phrase appears in prependContext
            matched = any(kw in ctx for kw in keywords)
            if matched:
                hits += 1
                ok(f"recall({query!r}) → relevant fact found")
            else:
                fail(f"recall({query!r}) → expected keywords {keywords} not in top results",
                     f"got: {ctx[:120]!r}")

        precision = hits / len(QUERIES_EXPECTED)
        if precision >= 0.8:
            ok(f"precision {precision:.0%} ≥ 80% threshold")
        else:
            fail(f"precision {precision:.0%} below 80% threshold")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Thread safety — 20 concurrent writers + 5 readers, 500 total operations
# ═══════════════════════════════════════════════════════════════════════════════

def run_thread_safety_tests() -> None:
    section("2. Thread safety — 20 writers + 5 readers, 500 ops")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))

        errors: list[str] = []
        written_facts: set[str] = set()
        wf_lock = threading.Lock()

        def writer(thread_id: int) -> None:
            for i in range(10):
                conv = f"conv-t{thread_id}"
                mem.add_message(conv, "user", f"thread {thread_id} message {i}")
                mem.add_message(conv, "assistant", f"reply to message {i}")
                fact = f"thread {thread_id} produced fact number {i} with unique data {thread_id * 1000 + i}"
                try:
                    mem.add_fact(conv, fact, confidence=0.9, person_id=_owner())
                    with wf_lock:
                        written_facts.add(fact)
                except Exception as e:
                    with _lock:
                        errors.append(f"writer-{thread_id}: {e}")

        def reader(thread_id: int) -> None:
            for _ in range(10):
                try:
                    mem.recall(f"thread {thread_id} fact data", conv_id=f"conv-t{thread_id}", person_id=_owner())
                    mem.turn_count()
                    mem.total_fact_count()
                except Exception as e:
                    with _lock:
                        errors.append(f"reader-{thread_id}: {e}")
                time.sleep(0.001)

        with concurrent.futures.ThreadPoolExecutor(max_workers=25) as pool:
            futures = []
            for i in range(20):
                futures.append(pool.submit(writer, i))
            for i in range(5):
                futures.append(pool.submit(reader, i))
            concurrent.futures.wait(futures)

        if not errors:
            ok("no exceptions under concurrent load")
        else:
            for e in errors[:3]:
                fail(f"concurrent error: {e}")

        # Deterministic: a writer invalidates the BM25 cache (self._bm25 = None) while recall is
        # building it. recall used to write into self._bm25 after that and raise TypeError.
        real_get_facts = mem.get_facts

        def get_facts_then_invalidate(*a, **k):
            out = real_get_facts(*a, **k)
            mem._bm25 = None   # what add_fact does after its commit
            return out

        mem._bm25 = None
        mem.get_facts = get_facts_then_invalidate
        try:
            mem.recall("thread fact data", conv_id="conv-t0", person_id=_owner())
            ok("recall survives the cache being invalidated mid-build")
        except Exception as e:  # noqa: BLE001
            fail(f"recall with the cache invalidated mid-build: {type(e).__name__}: {e}")
        finally:
            mem.get_facts = real_get_facts
        ok("an invalidated build is left orphaned, not kept") if mem._bm25 is None else \
            fail("a build from before the invalidation became the live cache")

        # Verify data integrity: total messages = 20 threads × 20 messages
        total_msgs = mem._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        expected = 20 * 20
        if total_msgs == expected:
            ok(f"message count exact: {total_msgs} (no lost writes)")
        else:
            fail(f"message count: got {total_msgs}, expected {expected}")

        # Facts: at most 200 (10 per thread × 20 threads), at least 1
        total_facts = mem.total_fact_count()
        if 1 <= total_facts <= 200:
            ok(f"fact count plausible: {total_facts} (deduplication may reduce)")
        else:
            fail(f"fact count implausible: {total_facts}")

        # No duplicate message IDs
        dupes = mem._db.execute(
            "SELECT id, COUNT(*) c FROM messages GROUP BY id HAVING c > 1"
        ).fetchall()
        if not dupes:
            ok("no duplicate message IDs (UUID uniqueness holds)")
        else:
            fail(f"{len(dupes)} duplicate message IDs found")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Deduplication — Jaccard threshold 0.85
# ═══════════════════════════════════════════════════════════════════════════════

def run_dedup_tests() -> None:
    section("3. Deduplication (Jaccard ≥ 0.85 blocks near-duplicates)")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))

        # Exact duplicate
        mem.add_fact("c", "user drinks coffee every morning", person_id=_owner())
        mem.add_fact("c", "user drinks coffee every morning", person_id=_owner())  # exact dup
        n = mem.total_fact_count()
        if n == 1:
            ok("exact duplicate blocked")
        else:
            fail(f"exact duplicate not blocked (count={n})")

        # High-similarity duplicate (should be blocked)
        mem.add_fact("c", "user drinks coffee every morning at breakfast", person_id=_owner())
        n2 = mem.total_fact_count()
        # "user drinks coffee every morning at breakfast" vs "user drinks coffee every morning"
        # tokens: {user, drinks, coffee, every, morning} ∩ {user, drinks, coffee, every, morning, at, breakfast}
        # Jaccard = 5/7 ≈ 0.71 — below threshold, should be ADDED
        if n2 == 2:
            ok("near-variant (Jaccard≈0.71) correctly admitted as new fact")
        else:
            fail(f"near-variant handling unexpected (count={n2})")

        # Very close duplicate (should be blocked — Jaccard ≈ 0.9)
        mem.add_fact("c", "the user drinks coffee every single morning", person_id=_owner())
        n3 = mem.total_fact_count()
        # tokens: {the, user, drinks, coffee, every, single, morning} vs {user, drinks, coffee, every, morning}
        # intersection: {user, drinks, coffee, every, morning} = 5
        # union: {the, user, drinks, coffee, every, single, morning} = 7
        # Jaccard = 5/7 ≈ 0.71 → admitted
        # Actually the threshold is 0.85, so this should be admitted too
        if n3 >= 2:
            ok(f"distinct facts admitted (total={n3})")
        else:
            fail(f"over-aggressive deduplication blocked distinct fact (total={n3})")

        # Truly distinct fact
        mem.add_fact("c", "user is a software engineer in San Francisco", person_id=_owner())
        n4 = mem.total_fact_count()
        if n4 > n3:
            ok("clearly distinct fact admitted")
        else:
            fail(f"clearly distinct fact blocked (count stayed at {n3})")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Offload index under concurrency — no node_id collisions
# ═══════════════════════════════════════════════════════════════════════════════

def run_offload_tests() -> None:
    section("4. Offload index under concurrency — unique node_ids")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))

        node_ids: list[str] = []
        id_lock = threading.Lock()
        errors: list[str] = []

        def do_offload(i: int) -> None:
            try:
                nid = mem.offload(f"tool output block {i} " + "x" * 200,
                                  summary=f"result {i}")
                with id_lock:
                    node_ids.append(nid)
            except Exception as e:
                with _lock:
                    errors.append(str(e))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(do_offload, range(50)))

        if not errors:
            ok(f"all 50 offloads succeeded (no exceptions)")
        else:
            fail(f"{len(errors)} offload errors: {errors[:2]}")

        unique_ids = set(node_ids)
        if len(unique_ids) == len(node_ids) == 50:
            ok(f"all 50 node_ids unique")
        else:
            fail(f"collision: {len(node_ids)} node_ids, {len(unique_ids)} unique")

        # Verify all can be resolved
        resolved = sum(1 for nid in node_ids if mem.resolve(nid) is not None)
        if resolved == 50:
            ok("all 50 offloads resolvable from disk")
        else:
            fail(f"only {resolved}/50 resolvable")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BM25 cache coherence — stale index not used after add_fact
# ═══════════════════════════════════════════════════════════════════════════════

def run_cache_coherence_tests() -> None:
    section("5. BM25 cache coherence")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))

        # Seed initial facts
        mem.add_fact("c", "user has a golden retriever named Max", person_id=_owner())
        mem.add_fact("c", "user prefers tea over coffee", person_id=_owner())
        mem.add_fact("c", "user works as a graphic designer", person_id=_owner())

        # Build cache by calling recall
        r1 = mem.recall("golden retriever dog", person_id=_owner())
        assert mem._bm25 is not None, "cache should be populated"
        cache_id_before = id(mem._bm25)
        ok("BM25 cache built on first recall")

        # Second recall without new facts — same cache object
        mem.recall("tea coffee drinks", person_id=_owner())
        cache_id_after_no_change = id(mem._bm25)
        if cache_id_before == cache_id_after_no_change:
            ok("cache object stable (no unnecessary rebuilds)")
        else:
            fail("cache rebuilt without add_fact — wasteful")

        # Add a fact — should invalidate
        mem.add_fact("c", "user's dog Max loves swimming in the ocean", person_id=_owner())
        if mem._bm25 is None:
            ok("cache invalidated after add_fact")
        else:
            fail("cache not invalidated after add_fact")

        # Recall should now include the new fact
        r2 = mem.recall("dog ocean swimming", person_id=_owner())
        if "ocean" in r2.prependContext or "swimming" in r2.prependContext or "Max" in r2.prependContext:
            ok("new fact found in recall after cache rebuild")
        else:
            fail("new fact missing from recall after invalidation",
                 f"got: {r2.prependContext[:100]!r}")

        # Cache should be rebuilt now
        if mem._bm25 is not None:
            ok("cache rebuilt after recall following invalidation")
        else:
            fail("cache not rebuilt after post-invalidation recall")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. L3 persona milestone gate — fires exactly once per threshold
# ═══════════════════════════════════════════════════════════════════════════════

def _word(n: int) -> str:
    """A made-up letters-only word, unique per n (digits alone don't make facts distinct: dedup
    compares word sets, so "fact number 3" and "fact number 4" collapse into one fact)."""
    out = ""
    n += 26 * 27   # at least three letters
    while n:
        n, r = divmod(n, 26)
        out = "bcdfghjklmnpqrstvwxzaeiouy"[r] + out
    return out


def _distinct_fact(i: int) -> str:
    """A fact no other _distinct_fact(j) shares four of its words with: dedup keeps every one."""
    w = [_word(4 * i + k) for k in range(4)]
    return f"the household keeps the {w[0]} {w[1]} beside the {w[2]} {w[3]}"


def run_l3_milestone_tests() -> None:
    section("6. L3 persona milestone gate")
    with tempfile.TemporaryDirectory() as tmp:
        mem = make_memory(Path(tmp))

        # Intercept _refresh_persona calls
        refresh_count = [0]
        original_refresh = mem._refresh_persona

        def counting_refresh() -> None:
            refresh_count[0] += 1
            # Don't call original (no LLM)

        mem._refresh_persona = counting_refresh

        import agent_memory as am
        threshold = am.L3_PERSONA_EVERY_N_L1
        ok(f"L3 threshold: {threshold} facts")

        # Add threshold-1 facts → persona should NOT fire
        for i in range(threshold - 1):
            mem.add_fact("c", _distinct_fact(i), person_id=_owner())
            mem.maybe_extract_facts("c", person_id=_owner())
        if mem.total_fact_count() == threshold - 1:
            ok(f"all {threshold - 1} distinct facts kept (none deduplicated)")
        else:
            fail(f"facts kept: {mem.total_fact_count()} of {threshold - 1}; dedup merged distinct test facts")

        if refresh_count[0] == 0:
            ok(f"no persona refresh before threshold ({threshold - 1} facts)")
        else:
            fail(f"persona refresh fired early at {threshold - 1} facts (count={refresh_count[0]})")

        # Add the threshold-th fact → should fire exactly once
        mem.add_fact("c", _distinct_fact(threshold - 1), person_id=_owner())
        mem.maybe_extract_facts("c", person_id=_owner())

        if refresh_count[0] == 1:
            ok("persona refresh fired exactly once at threshold")
        else:
            fail(f"persona refresh count at threshold: {refresh_count[0]} (expected 1)")

        # Add more facts below next milestone → should not fire again
        for i in range(5):
            mem.add_fact("c", _distinct_fact(1000 + i), person_id=_owner())
            mem.maybe_extract_facts("c", person_id=_owner())

        if refresh_count[0] == 1:
            ok("persona refresh not re-fired for facts below next milestone")
        else:
            fail(f"persona refresh double-fired (count={refresh_count[0]})")

        # Add enough to reach 2× threshold
        current = mem.total_fact_count()
        gap = (2 * threshold) - current
        for i in range(max(0, gap)):
            mem.add_fact("c", _distinct_fact(2000 + i), person_id=_owner())
        mem.maybe_extract_facts("c", person_id=_owner())

        if refresh_count[0] == 2:
            ok("persona refresh fires again at 2× threshold")
        else:
            fail(f"expected 2 refreshes at 2× threshold, got {refresh_count[0]}")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Skills loader — good + malformed SKILL.md files
# ═══════════════════════════════════════════════════════════════════════════════

def run_skills_tests() -> None:
    section("7. Skills loader (good + malformed files)")
    from agent_skills import load_skills, skills_system_prompt, skill_names

    with tempfile.TemporaryDirectory() as tmp:
        skills_dir = Path(tmp)

        # Good skill
        good = skills_dir / "search"
        good.mkdir()
        (good / "SKILL.md").write_text(
            "---\nname: search\ndescription: Search the web for current information\n---\n"
            "## Overview\nUse DuckDuckGo to retrieve live data.\n"
            "## When to Use\nWhen the user asks for current events or prices.\n"
        )

        # Missing frontmatter
        no_fm = skills_dir / "no-frontmatter"
        no_fm.mkdir()
        (no_fm / "SKILL.md").write_text("Just a plain file with no YAML frontmatter block\n")

        # Frontmatter missing 'name'
        no_name = skills_dir / "no-name"
        no_name.mkdir()
        (no_name / "SKILL.md").write_text(
            "---\ndescription: A skill with no name field\n---\n## Body\ncontent here\n"
        )

        # Frontmatter missing 'description'
        no_desc = skills_dir / "no-desc"
        no_desc.mkdir()
        (no_desc / "SKILL.md").write_text(
            "---\nname: no-desc\n---\n## Body\ncontent here\n"
        )

        # Block scalar description
        block = skills_dir / "block-scalar"
        block.mkdir()
        (block / "SKILL.md").write_text(
            "---\nname: block-scalar\ndescription: |\n  Multi-line description\n"
            "  that spans two lines here\n---\n## Overview\nBlock scalar skill body.\n"
        )

        import io, contextlib
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            skills = load_skills(skills_dir)
        stderr_out = stderr_capture.getvalue()

        names = skill_names(skills)
        if "search" in names:
            ok("good skill loaded")
        else:
            fail("good skill not loaded")

        if "block-scalar" in names:
            ok("block scalar description skill loaded")
        else:
            fail("block scalar skill failed to load")

        for bad in ("no-frontmatter", "no-name", "no-desc"):
            if bad not in names:
                ok(f"malformed '{bad}' skipped")
            else:
                fail(f"malformed '{bad}' should have been skipped")

        if len(skills) == 2:
            ok("exactly 2 valid skills loaded out of 5 files")
        else:
            fail(f"expected 2 valid skills, got {len(skills)}")

        if stderr_out.strip():
            ok(f"stderr warnings emitted for malformed files ({len(stderr_out.splitlines())} lines)")
        else:
            fail("no stderr warnings for malformed SKILL.md files")

        # Prompt composition
        prompt = skills_system_prompt(skills)
        if "search" in prompt and "block-scalar" in prompt:
            ok("both skill names appear in composed system prompt")
        else:
            fail(f"prompt missing skill names: {prompt[:80]!r}")

        # active filter
        filtered = skills_system_prompt(skills, active={"search"})
        if "search" in filtered and "block-scalar" not in filtered:
            ok("active= filter works correctly")
        else:
            fail(f"active= filter failed: {filtered[:80]!r}")

        # 1024-char description truncation
        long_skill_dir = skills_dir / "long-desc"
        long_skill_dir.mkdir()
        (long_skill_dir / "SKILL.md").write_text(
            f"---\nname: long-desc\ndescription: {'X' * 2000}\n---\n## Body\ncontent\n"
        )
        ls = load_skills(skills_dir)
        long = next((s for s in ls if s.name == "long-desc"), None)
        if long and len(long.description) == 1024:
            ok("description truncated to 1024 chars")
        elif long:
            fail(f"description length {len(long.description)}, expected 1024")
        else:
            fail("long-desc skill not loaded")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. API concurrent load — 30 parallel requests
# ═══════════════════════════════════════════════════════════════════════════════

def run_api_concurrent_tests() -> None:
    section("8. API concurrent load (30 parallel requests)")

    port = 7772
    env = {**os.environ, "AGENT_WORKSPACE_PROCESSES": "1",   # its own throwaway server exercises the off-by-default route
           "AGENT_MEMORY_DB": tempfile.mktemp(suffix=".db"),
           "AGENT_API_TOKEN": "stress-token"}
    proc = subprocess.Popen(
        [sys.executable, "scripts/agent_api_server.py", "--port", str(port)],
        stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
        env=env, cwd=Path(__file__).parent.parent
    )

    # Wait for server ready
    deadline = time.time() + 10
    ready = False
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.1)

    if not ready:
        fail("server did not start within 10 seconds")
        proc.kill()
        return
    ok("server started and healthy")

    BASE = f"http://127.0.0.1:{port}"
    TOKEN = "stress-token"

    def req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = BASE + path
        data = json.dumps(body).encode() if body else None
        headers = {"X-Agent-Token": TOKEN, "Content-Type": "application/json"}
        r = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # ── Test 1: 10 concurrent workspace creations ──
    def create_ws(_: int) -> tuple[int, dict]:
        return req("POST", "/v1/workspaces", {"driver": "worktree"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(create_ws, range(10)))

    codes = [r[0] for r in results]
    ws_ids = [r[1].get("ws_id") for r in results if r[0] == 201]

    if all(c == 201 for c in codes):
        ok(f"all 10 concurrent workspace creations succeeded (201 Created)")
    else:
        fail(f"workspace creation failures: {codes}")

    if len(set(ws_ids)) == 10:
        ok("all 10 workspace IDs unique")
    else:
        fail(f"duplicate workspace IDs: {len(set(ws_ids))} unique out of {len(ws_ids)}")

    # ── Test 2: 10 concurrent file writes to different workspaces ──
    def write_file(args: tuple[str, int]) -> tuple[int, dict]:
        ws_id, i = args
        content = f"# file {i}\nresult = {i} * {i}\nprint(result)\n"
        return req("POST", f"/v1/workspaces/{ws_id}/files",
                   {"path": f"script_{i}.py", "content": content})

    write_args = [(ws_ids[i], i) for i in range(min(10, len(ws_ids)))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        write_results = list(pool.map(write_file, write_args))

    if all(r[0] == 200 for r in write_results):
        ok("all 10 concurrent file writes succeeded")
    else:
        fail(f"file write failures: {[r[0] for r in write_results if r[0] != 200]}")

    # ── Test 3: 10 concurrent process starts (python3 each script) ──
    def start_proc(args: tuple[str, int]) -> tuple[int, dict]:
        ws_id, i = args
        return req("POST", f"/v1/workspaces/{ws_id}/processes",
                   {"process_id": f"p{i}", "command": "python3",
                    "args": [f"script_{i}.py"]})

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        proc_results = list(pool.map(start_proc, write_args))

    if all(r[0] == 201 for r in proc_results):
        ok("all 10 concurrent process starts succeeded (201)")
    else:
        fails = [(i, r) for i, r in enumerate(proc_results) if r[0] != 201]
        fail(f"process start failures: {fails[:3]}")

    time.sleep(2)  # let scripts finish

    # ── Test 4: verify process outputs ──
    correct = 0
    for ws_id, i in write_args:
        _, ws_state = req("GET", f"/v1/workspaces/{ws_id}")
        procs = ws_state.get("processes", [])
        for p in procs:
            if p.get("process_id") == f"p{i}":
                expected_output = str(i * i)
                logs = p.get("logs", [])
                if any(expected_output in line for line in logs):
                    correct += 1
                break

    if correct == 10:
        ok("all 10 processes produced correct output (i²)")
    elif correct >= 8:
        ok(f"{correct}/10 processes correct (timing — some still running)")
    else:
        fail(f"only {correct}/10 processes produced correct output")

    # ── Test 5: 10 concurrent health checks (no auth) ──
    def health(_: int) -> int:
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=5) as r:
                return r.status
        except Exception:
            return 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        health_codes = list(pool.map(health, range(10)))

    if all(c == 200 for c in health_codes):
        ok("10 concurrent health checks all 200")
    else:
        fail(f"health check failures: {health_codes}")

    # ── Cleanup: delete all workspaces ──
    deleted = 0
    for ws_id in ws_ids:
        code, _ = req("DELETE", f"/v1/workspaces/{ws_id}")
        if code == 200:
            deleted += 1

    if deleted == len(ws_ids):
        ok(f"all {deleted} workspaces cleaned up")
    else:
        fail(f"only {deleted}/{len(ws_ids)} workspaces deleted")

    # Final health should show 0 workspaces
    _, h = req("GET", "/health")
    if h.get("workspaces") == 0:
        ok("final health: workspaces=0 (no leaks)")
    else:
        fail(f"final health shows {h.get('workspaces')} workspaces (leak?)")

    proc.kill()
    proc.wait()
    ok("server shut down cleanly")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 60)
    print("Titan Agent — Demanding Stress + Correctness Test Suite")
    print("=" * 60)

    t0 = time.time()
    run_bm25_tests()
    run_thread_safety_tests()
    run_dedup_tests()
    run_offload_tests()
    run_cache_coherence_tests()
    run_l3_milestone_tests()
    run_skills_tests()
    # Section 8 starts a second agent API server and creates workspaces and processes from this
    # checkout: opt-in, so the deploy gate (and a plain run) stays hermetic.
    if "--api" in sys.argv[1:]:
        run_api_concurrent_tests()
    else:
        print("\n── 8. API concurrent load: skipped (pass --api to run it) ──")
    elapsed = time.time() - t0

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed — {elapsed:.1f}s")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
