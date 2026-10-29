#!/usr/bin/env python3
"""
Unit tests for the per-turn gates: the Titan-warnings intent check and the Jev turn gates
(AGENT_JEV_TURN_GATES) for fact saving and skill choice. Hermetic: Jev is faked, nothing is saved.

Run: python3 scripts/test_turn_gates.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


class _FakeJev:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or {}, fail, 0

    def decide(self, state, questions, **kw):
        self.calls += 1
        self.origin = kw.get("origin")
        if self.fail:
            raise RuntimeError("network down")
        return {"answers": {q: self.answers[q] for q in questions}, "_meta": {"latency_ms": 1, "cost_usd": 0.0}}


def run():
    ok = True
    orig_client, orig_index = lib._jev_client, lib._kb_index
    saved = []
    lib._kb_index = lambda sid, title, text, stype, md=None, **kw: saved.append((sid, title, md)) or {}
    reply = "Got it — I'll keep that in mind for next time you ask."
    try:
        print("\n── Titan warnings intent ──")
        for msg, want in [("any warnings?", True), ("Any warnings on titan right now?", True),
                          ("show me the gpu warnings", True), ("warnings", True),
                          ("What does the warning light on my dishwasher mean?", False),
                          ("is there a weather warning for tomorrow in the bay area", False),
                          ("any issues with the server?", False)]:
            ok &= check(msg, lib.asks_titan_warnings(msg.lower()), want)

        os.environ["AGENT_JEV_TURN_GATES"] = "0"
        print("\n── Gates off → only explicit requests save, Jev never asked ──")
        fake = _FakeJev({"lasting_fact": {"type": "noul", "noul": 0.01}})
        lib._jev_client = lambda: fake
        saved.clear()
        lib._maybe_save_fact("who is my dentist?", reply)
        ok &= check("'who is my dentist?' not saved", len(saved), 0)
        lib._maybe_save_fact("Remember the plumber is AquaFix", reply)
        ok &= check("'Remember …' saved", len(saved), 1)
        ok &= check("stable id from the statement", bool(saved) and saved[0][0].startswith("note-said-"), True)
        lib._maybe_save_fact("remember the plumber is AquaFix ", reply)
        ok &= check("same statement → same id", len({s[0] for s in saved}), 1)
        ok &= check("no Jev call", fake.calls, 0)
        saved.clear()
        lib._maybe_save_fact("Remember the plumber is AquaFix", reply, noted=True)
        ok &= check("the turn saved a cleaned note: no raw copy (10-02 audit)", len(saved), 0)
        ok &= check("skill: no verdict", lib.jev_pick_skill("remind me Tuesday", {"calendar": "events"}), None)

        os.environ["AGENT_JEV_TURN_GATES"] = "1"
        print("\n── Gates on: fact saving ──")
        for label, p, msg, want in [("question not saved", 0.03, "who is my dentist?", 0),
                                    # 10-03 KB cleanup: Jev's "lasting fact" verdict alone never saves (only an explicit request)
                                    ("a lasting-fact verdict alone saves nothing", 0.97,
                                     "The kids' school pickup is at 3:15 on weekdays now", 0),
                                    ("unsure (0.4) not saved", 0.4, "my plan is to repaint", 0)]:
            lib._jev_client = lambda p=p: _FakeJev({"lasting_fact": {"type": "noul", "noul": p}})
            saved.clear()
            lib._maybe_save_fact(msg, reply)
            ok &= check(label, len(saved), want)
        lib._jev_client = lambda: _FakeJev(fail=True)
        saved.clear()
        lib._maybe_save_fact("remember the gate code is 1234", reply)
        ok &= check("Jev error → explicit request still saves", len(saved), 1)
        lib._jev_client = lambda: None
        saved.clear()
        lib._maybe_save_fact("thanks", reply)
        ok &= check("no key + not explicit → nothing saved", len(saved), 0)

        print("\n── Gates on: skill choice ──")
        skills = {"calendar": "events and reminders", "media": "Plex playback", "web-research": "current info"}
        for label, choice, conf, want in [("confident pick", "calendar", 0.98, "calendar"),
                                          ("confident none", "none", 0.9, ""),
                                          ("unsure → no verdict", "media", 0.45, None),
                                          ("insufficient → no verdict", "insufficient_information", 0.9, None),
                                          ("unknown skill → no verdict", "home-automation", 0.99, None)]:
            lib._jev_client = lambda c=choice, f=conf: _FakeJev({"skill": {"type": "choice", "choice": c, "confidence": f}})
            ok &= check(label, lib.jev_pick_skill("remind me Tuesday", skills), want)
        lib._jev_client = lambda: _FakeJev(fail=True)
        ok &= check("error → no verdict", lib.jev_pick_skill("remind me Tuesday", skills), None)

        print("\n── Gates on: find_files best match ──")
        paths = ["/p/tax/notes.md", "/Downloads/2025-tax-return.pdf", "/p/tax/schema.json", "/Documents/tax-2019.pdf"]
        lib._jev_client = lambda: _FakeJev({"file": {"type": "choice", "choice": "2", "confidence": 0.93}})
        out = lib._pick_best_file("my 2025 tax return", paths)
        ok &= check("confident pick is named first", out.startswith("Best match: /Downloads/2025-tax-return.pdf"), True)
        ok &= check("full list still included", all(p in out for p in paths), True)
        lib._jev_client = lambda: _FakeJev({"file": {"type": "choice", "choice": "2", "confidence": 0.4}})
        ok &= check("unsure → plain list", lib._pick_best_file("tax", paths), "\n".join(paths))
        lib._jev_client = lambda: _FakeJev({"file": {"type": "choice", "choice": "insufficient_information", "confidence": 0.9}})
        ok &= check("no clear match → plain list", lib._pick_best_file("tax", paths), "\n".join(paths))
        lib._jev_client = lambda: _FakeJev(fail=True)
        ok &= check("Jev error → plain list", lib._pick_best_file("tax", paths), "\n".join(paths))
        os.environ["AGENT_JEV_TURN_GATES"] = "0"
        ok &= check("gates off → plain list, no call", lib._pick_best_file("tax", paths), "\n".join(paths))
        os.environ["AGENT_JEV_TURN_GATES"] = "1"

        print("\n── Decision origin (X-Decision-* headers for the broker's readouts) ──")
        io = lib._TURN_IO
        saved_env = os.environ.pop("AGENT_TURN_SOURCE", None)
        try:
            for attr in ("source", "person", "role", "jev_source"):
                setattr(io, attr, None)
            ok &= check("CLI / background turn: source only", lib.jev_origin(), {"source": "user"})
            io.person, io.role = {"id": "p_4dfb9c5a", "name": "Robin"}, "adult"
            ok &= check("a person's turn: id and role",
                        lib.jev_origin(), {"source": "user", "person": "p_4dfb9c5a", "role": "adult"})
            io.person, io.role = {"id": "kitchen", "name": "Kitchen"}, "household"
            ok &= check("a household device: role only", lib.jev_origin(), {"source": "user", "role": "household"})
            io.person, io.role, io.source = None, None, "eval"
            ok &= check("an eval turn", lib.jev_origin(), {"source": "eval"})
            io.source, io.jev_source = None, "proactive"
            ok &= check("the proactive loop labels its own", lib.jev_origin(), {"source": "proactive"})
            io.jev_source, io.person, io.role = None, {"id": "zach", "name": "Zach"}, "admin"
            fake = _FakeJev({"skill": {"type": "choice", "choice": "calendar", "confidence": 0.95}})
            lib._jev_client = lambda: fake
            lib.jev_pick_skill("what's on tomorrow?", {"calendar": "events", "media": "music"})
            ok &= check("a gate call carries the turn's origin", fake.origin,
                        {"source": "user", "person": "zach", "role": "admin"})
            import jev_client
            broker = {"provider": "broker", "base": "http://b", "key": ""}
            ok &= check("headers go to a broker", jev_client.origin_headers(fake.origin, broker),
                        {"X-Decision-Source": "user", "X-Decision-Person": "zach", "X-Decision-Role": "admin"})
            ok &= check("never to a provider", jev_client.origin_headers(fake.origin, {"provider": "openrouter"}), {})
        finally:
            for attr in ("source", "person", "role", "jev_source"):
                setattr(io, attr, None)
            if saved_env is not None:
                os.environ["AGENT_TURN_SOURCE"] = saved_env
    finally:
        lib._jev_client, lib._kb_index = orig_client, orig_index
        os.environ.pop("AGENT_JEV_TURN_GATES", None)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
