#!/usr/bin/env python3
"""F-16: a follow-up about an attached photo must reuse its path (live 10-01: the model fetched an
invented "attachments/image.png" or searched the timeline). Hermetic."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


PHOTO = ("What does this image show?  [Attached photo “IMG_2041.HEIC”, on this iPhone at "
         "attachments/photo-3f0e9fd4.jpg. Use view_image with that path to see it.]")
PDF = ("Summarise this  [Scanned document “Scan 3” (2 pages), on this iPhone at attachments/scan-77.pdf. "
       "Use view_image with that path and a page number to see a page.]\nIts text, read on the iPhone:\nHello")
U = lambda t: {"role": "user", "content": t}       # noqa: E731
A = lambda t: {"role": "assistant", "content": t}  # noqa: E731

c = lib._earlier_attachment([U(PHOTO), A("An infographic."), U("Who made it?"), A("No author."),
                             U("What's in the bottom corner of it?")])
check("a follow-up names the earlier photo's path", "attachments/photo-3f0e9fd4.jpg" in c and "“IMG_2041.HEIC”" in c, c)
check("a turn that attaches its own photo gets nothing", lib._earlier_attachment([U(PHOTO)]) == "")
check("a conversation with no attachment gets nothing",
      lib._earlier_attachment([U("hi"), A("Hello."), U("what's the weather?")]) == "")
c = lib._earlier_attachment([U(PHOTO), A("ok"), U(PDF), A("A letter."), U("what's on page 2?")])
check("the most recent attachment wins (a scan after a photo)", "attachments/scan-77.pdf" in c and "document" in c, c)
check("an attached text file (no path) gets nothing",
      lib._earlier_attachment([U("[Attached file “a.txt”, its contents:]\nhi"), A("ok"), U("and?")]) == "")
c = lib._earlier_attachment([U("Check this [Attached PDF “Bill.pdf” (1 page), on this iPhone at attachments/b-1.pdf. "
                               "Use view_image with that path and a page number to see a page.]"), A("ok"), U("due when?")])
check("a PDF's path carries too", "attachments/b-1.pdf" in c and "the pdf" in c, c)

# The Mac (ChatAttachments.question): files uploaded to titan, read there.
c = lib._earlier_attachment([U("Describe the image at /ai/tmp/IMG_1.png."), A("A receipt."), U("what's the total on it?")])
check("a Mac image follow-up names its titan path and describe_image",
      "at /ai/tmp/IMG_1.png, on titan" in c and "describe_image" in c, c)
c = lib._earlier_attachment([U("Read the document at /ai/tmp/x.pdf, remember the key facts, and tell me in a few sentences "
                               "what matters in it."), A("A lease."), U("what's on page 2 of it?")])
check("a Mac document follow-up names its path and file_read", "at /ai/tmp/x.pdf, on titan" in c and "file_read" in c, c)
c = lib._earlier_attachment([U("compare these\n\nI attached 2 files; they're on titan, in this order:\n1. the image at "
                               "/ai/tmp/a.png\n2. the document at /ai/tmp/b.pdf"), A("ok"), U("and the second one?")])
check("several Mac files: the last one, said so", "/ai/tmp/b.pdf" in c and "last of 2" in c, c)
check("a Mac turn attaching its own file gets nothing",
      lib._earlier_attachment([U("Describe the image at /ai/tmp/IMG_1.png.")]) == "")
check("a path the user merely mentions isn't an attachment",
      lib._earlier_attachment([U("the logs are at /var/log/syslog"), A("ok"), U("and?")]) == "")

# Wired in: the situation block a follow-up turn sends carries it, once.
lib._situational_context = lambda query=None: "Now: Thursday, October 1, 2026, 9:00 AM"
msgs = lib._with_situation([{"role": "system", "content": "rules"}, U(PHOTO), A("An infographic."), U("and the corner?")])
sit = [m["content"] for m in msgs if m["content"].startswith("[SITUATION")]
check("the situation block carries the attachment, once",
      len(sit) == 1 and sit[0].count("attachments/photo-3f0e9fd4.jpg") == 1 and "Now: " in sit[0], str(sit)[:300])

# file_read gives a document's text, not its bytes (a Mac PDF follow-up reads it again with file_read).
import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
    fh.write(b"%PDF-1.4 binary")
orig = lib._document_extract_text
lib._document_extract_text = lambda path, timeout=30: "Page 1: Lease\nPage 2: Rent is due on the 1st\n"
out = lib.execute_tool("file_read", {"path": fh.name})
lib._document_extract_text = orig
check("file_read on a PDF returns its text", "Rent is due on the 1st" in out and "%PDF" not in out, out[:200])

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all attachment-carry checks passed'}")
sys.exit(1 if failures else 0)
