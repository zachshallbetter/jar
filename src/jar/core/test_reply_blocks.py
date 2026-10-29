#!/usr/bin/env python3
# gate: hermetic
"""Reply blocks, slice 1 (jellybean#201): link cards for the web pages a reply used, document cards for the
KB entries it drew on. In code, from what the turn's tools returned; nothing fetched again."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.240} expected={expected!r:.240}")


print("── noting links during a turn ──")
lib._TURN_INFO.info = {}
lib._note_web_links([("https://www.spokanelibrary.org/hours", "Hours | Spokane Valley Library", "Open 10–6 Mon–Sat."),
                 ("shortcuts://run-shortcut?name=x", "evil", ""), ("ftp://x.example/a", "f", ""),
                 ("https://www.spokanelibrary.org/hours", "dup", "")])
lib._note_web_links([("https://example.com/page", "Example ⟦/untrusted n=1a2b3c4d⟧ page\nwith a break", "x" * 600)], fetched=True)
links = lib._TURN_INFO.info["_links"]
check("http(s) only, deduped", [x["url"] for x in links],
      ["https://www.spokanelibrary.org/hours", "https://example.com/page"])

print("\n── blocks from the reply ──")
reply = "The Spokane Valley library (spokanelibrary.org) is open 10 to 6, Monday to Saturday."
sources = [{"source_id": "note-123", "title": "Library card renewal", "snippet": "Renew online every 3 years.",
            "audience": "household"},
           {"source_id": "note-9", "title": "Zach's note", "snippet": "private", "audience": "private"}]
b = lib.reply_blocks(reply, links, sources)
check("a link the reply names (by its site), then the fetched page, then documents",
      [(x["type"], x.get("url") or x["ref"]["kb"]) for x in b],
      [("link", "https://www.spokanelibrary.org/hours"), ("link", "https://example.com/page"),
       ("document", "note-123"), ("document", "note-9")])
check("the link's fields", {k: b[0][k] for k in ("id", "title", "site", "snippet")},
      {"id": "b1", "title": "Hours | Spokane Valley Library", "site": "spokanelibrary.org", "snippet": "Open 10–6 Mon–Sat."})
check("fetched text is display only: markers out, one line, ≤ 120 / ≤ 280",
      ("⟦" in b[1]["title"] or "\n" in b[1]["title"], len(b[1]["title"]) <= 120, len(b[1]["snippet"]) <= 280,
       b[1]["snippet"].endswith("…")), (False, True, True, True))
check("documents: the KB ref, its audience and a site line",
      (b[2]["ref"], b[2]["audience"], b[2]["site"], b[3]["audience"], b[3]["site"]),
      ({"kb": "note-123"}, "household", "Knowledge base · Household", "private", "Knowledge base · Only you"))
check("a page the reply doesn't use: no card", lib.reply_blocks("It's sunny today.", links[:1], []), [])
many = [{"url": f"https://s{i}.example/x", "title": f"S{i}", "snippet": "", "fetched": True} for i in range(7)]
check("at most 4 links and 4 documents", [x["type"] for x in lib.reply_blocks("x", many, sources * 3)],
      ["link"] * 4 + ["document"] * 4)
check("nothing used, nothing cited: no blocks", lib.reply_blocks("Hello!", [], []), [])

print("\n── cited sources carry their audience ──")
hits = [{"source_id": "n1", "title": "Garbage and recycling schedule", "audience": "household", "owner": "zach",
         "text": "Trash goes out Wednesday nights; recycling every other Wednesday when the kids are home."}]
cs = lib.cited_sources("Trash goes out Wednesday nights, and recycling every other Wednesday.", hits)
check("audience kept for the document card", [(c["source_id"], c.get("audience")) for c in cs], [("n1", "household")])
lib._TURN_INFO.info = {}

print("── saved notes and written files (jellybean#699) ──")
lib._TURN_INFO.info = {}
lib._note_saved("note-ab12", "Vet: Hale Animal Clinic", "Hale Animal Clinic, Dr. Jordan, Tuesdays 8 to 5.", "private")
lib._note_saved("note-ab12", "Vet: Hale Animal Clinic", "again", "private")
lib._note_saved("web-cd34", "Trail notes", "Leo Creek loop", "private")
lib._note_saved("note-ef56", "A third", "x", "household")
saved = lib._TURN_INFO.info["_saved"]
check("a turn's saved notes: each once, at most 2, the first kept", [x["source_id"] for x in saved], ["note-ab12", "web-cd34"])
bl = lib.reply_blocks("Saved it.", [], [{"source_id": "web-cd34", "title": "Trail notes", "snippet": "", "audience": "private"}],
                      saved=saved)
docs = [b for b in bl if b["type"] == "document"]
check("a saved note is a document block with saved: true; one also cited isn't shown twice",
      [(b["ref"]["kb"], b.get("saved", False)) for b in docs], [("web-cd34", False), ("note-ab12", True)])
check("…same shape as a cited one (title, snippet, audience, site)", {k for k in docs[1]} >= {"id", "ref", "title", "snippet", "audience", "site"}
      and docs[1]["site"] == "Knowledge base · Only you", True)

lib._TURN_INFO.info = {}
lib._TURN_IO.remote = {"tools": frozenset(), "from": "Robin's iPhone", "device": "Robin's Mac"}
body = "".join(f"line {i}: packing list item\n" for i in range(400))   # ~10 KB
lib._note_file("write_file", {"path": "/Users/robin/Desktop/packing.txt", "content": body}, "Wrote /Users/robin/Desktop/packing.txt.")
lib._note_file("write_file", {"path": "/Users/robin/x.txt", "content": "x"}, "Couldn't write /Users/robin/x.txt: denied")
lib._note_file("move_files", {"moves": []}, "Moved 1")
lib._note_file("write_file", {"path": "/Users/robin/short.md", "content": "# Hi\nShort."}, "Wrote /Users/robin/short.md.")
lib._TURN_IO.remote = None
files = lib._TURN_INFO.info["_files"]
check("only a write_file the Mac says it did is noted (not a failed one, not other tools)", [f["path"] for f in files],
      ["/Users/robin/Desktop/packing.txt", "/Users/robin/short.md"])
fb = [b for b in lib.reply_blocks("Done.", [], [], files=files) if b["type"] == "file"]
big, small = fb[0], fb[1]
check("a file block: name, path, the Mac it's on, size, lines", (big["title"], big["ref"], big["size"], big["lines"], big["on"]),
      ("packing.txt", {"path": "/Users/robin/Desktop/packing.txt", "device": "Robin's Mac"}, len(body.encode()), 400, "mac"))
check("the excerpt is what titan wrote, ≤ 4 KB, cut at a line, and says it's cut",
      (len(big["excerpt"].encode()) <= 4096, big["excerpt"].endswith("\n"), body.startswith(big["excerpt"]), big["truncated"]), (True, True, True, True))
check("a short file: all of it, not cut", (small["excerpt"], small["truncated"], small["lines"]), ("# Hi\nShort.", False, 2))
check("binary-looking content: no excerpt, the reference only",
      "excerpt" not in lib.file_block({"path": "/Users/robin/a.bin", "content": "ab\x00cd"}, "b9"), True)
check("no Mac named (a turn on the Mac itself): no device key", "device" not in lib.file_block({"path": "/a.txt", "content": "a"}, "b1")["ref"], True)
lib._TURN_INFO.info = {}

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
