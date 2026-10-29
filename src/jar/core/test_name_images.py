#!/usr/bin/env python3
"""
Unit tests for name_images: names for many pictures on the Mac from what's in them, proposed as
moves for move_files (09-30: "rename the Gemini captures" renamed none of them). Hermetic: the
Mac (fetch_image) and Gemini are faked.

Run: python3 scripts/test_name_images.py
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib
import household_identity as hid


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def moves_of(out):
    tail = out.split("move_files with moves=", 1)
    return json.loads(tail[1]) if len(tail) == 2 else None


def run():
    ok = True
    seen, sent = [], []
    missing = {"~/Downloads/gone.png"}

    listings = {"~/Downloads": "[~/Downloads: 3 items, showing 3]\n"
                                "jellybean-mascot-waving.png · 210 KB · PNG image · Today 9:12 AM\n"
                                "Gemini_Generated_Image_c3d4.png · 1.2 MB · PNG image · Today 9:10 AM\n"
                                "DSC_0042.NEF · 61.4 MB · Nikon raw image · Yesterday 4:02 PM"}

    def device(req):
        seen.append(req)
        if req["tool"] == "list_files":
            return {"result": listings.get(req["args"]["path"], "No such folder")}
        if req["args"]["path"] in missing:
            return {"error": "File not found"}
        return {"result": json.dumps({"mime": "image/png", "width": 512, "height": 512, "data_base64": "SU1H"})}

    replies = []

    def gemini(model, parts, gen_config=None, **kw):
        sent.append((parts, gen_config))
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(replies.pop(0))}]}}]}

    orig_gem = lib._gemini
    lib._gemini, lib._TURN_IO.device = gemini, device
    try:
        print("\n── names from what's in each picture ──")
        replies[:] = [["A golden retriever on a beach", "Jellybean mascot waving"]]
        out = lib.execute_tool("name_images", {"paths": json.dumps([
            "~/Downloads/Gemini_Generated_Image_a1b2.PNG", "~/Downloads/gone.png",
            "~/Downloads/Gemini_Generated_Image_c3d4.png"])})
        ok &= check("old → new, its own extension, never an existing file's name",
                    [l for l in out.splitlines() if "→" in l],
                    ["- Gemini_Generated_Image_a1b2.PNG → golden-retriever-on-beach.PNG",
                     "- Gemini_Generated_Image_c3d4.png → jellybean-mascot-waving-2.png"])
        ok &= check("nothing renamed yet, and says so", "nothing renamed yet" in out.splitlines()[0], True)
        ok &= check("a missing file is named as skipped", any(l.startswith("- ~/Downloads/gone.png:") and "File not found" in l for l in out.splitlines()), True)
        ok &= check("moves ready for move_files, same folder", moves_of(out), [
            {"from": "~/Downloads/Gemini_Generated_Image_a1b2.PNG", "to": "~/Downloads/golden-retriever-on-beach.PNG"},
            {"from": "~/Downloads/Gemini_Generated_Image_c3d4.png", "to": "~/Downloads/jellybean-mascot-waving-2.png"}])
        ok &= check("the Mac sends small copies", {(r["tool"], r["args"]["max_px"]) for r in seen if r["tool"] == "fetch_image"},
                    {("fetch_image", 512)})
        ok &= check("one listing per folder", [r["args"]["path"] for r in seen if r["tool"] == "list_files"], ["~/Downloads"])
        ok &= check("one vision call for both, asking for JSON",
                    (len(sent), sent[0][1], sum(1 for p in sent[0][0] if "inline_data" in p)),
                    (1, {"responseMimeType": "application/json"}, 2))
        ok &= check("image data never in the reply", "SU1H" in out, False)
        ok &= check("reads only: no move or write reached the Mac", {r["tool"] for r in seen}, {"fetch_image", "list_files"})

        print("\n── private text, dates, big files ──")
        sent.clear()
        replies[:] = [[{"name": "wifi password Bluebird42", "kind": "handwritten note"},
                       {"name": "visa card 4111 1111 1111 1111", "kind": "credit card"},
                       {"name": "kitchen whiteboard chores", "kind": "whiteboard"},
                       {"name": "birthday card from Ava", "kind": "greeting card"}]]
        out = lib.execute_tool("name_images", {"paths": [
            "~/Downloads/IMG_7781.HEIC", "~/Downloads/IMG_7782.HEIC",
            "~/Downloads/Screenshot 2026-09-30 at 3.14.15 PM.png", "~/Downloads/IMG_7790.HEIC",
            "~/Downloads/DSC_0042.NEF"]})
        tos = [m["to"] for m in moves_of(out)]
        ok &= check("a password or card number never reaches a name: its kind instead", tos[:2],
                    ["~/Downloads/handwritten-note.HEIC", "~/Downloads/credit-card.HEIC"])
        ok &= check("… and it says why", out.count("(a plain name: the picture shows something private)"), 2)
        ok &= check("a screenshot keeps its date", tos[2], "~/Downloads/kitchen-whiteboard-chores-2026-09-30.png")
        ok &= check("an ordinary 'card' stays a name", tos[3], "~/Downloads/birthday-card-from-ava.HEIC")
        ok &= check("over 40 MB: skipped, never fetched or sent",
                    ("- ~/Downloads/DSC_0042.NEF: 61 MB, too big to send; name it yourself" in out,
                     any(r["args"].get("path") == "~/Downloads/DSC_0042.NEF" for r in seen if r["tool"] == "fetch_image")),
                    (True, False))
        ok &= check("vision is told to describe, not copy, private text",
                    "Never copy text from the image that could be private" in sent[0][0][-1]["text"], True)

        print("\n── look-alikes, already-named, the user's style ──")
        sent.clear()
        replies[:] = [["Sunset over the lake", "sunset over lake", "Kitchen photo"]]
        out = lib.execute_tool("name_images", {"paths": ["~/Pictures/IMG_1.jpg", "~/Pictures/IMG_2.jpg",
                                                         "~/Pictures/kitchen.jpg"], "style": "short, no articles"})
        ok &= check("two alike → -2", [m["to"] for m in moves_of(out)],
                    ["~/Pictures/sunset-over-lake.jpg", "~/Pictures/sunset-over-lake-2.jpg"])
        ok &= check("a name it already has isn't a move", "- kitchen.jpg: already named for what's in it" in out, True)
        ok &= check("the user's style reaches vision", "The user asked for: short, no articles." in sent[0][0][-1]["text"], True)

        print("\n── many at once ──")
        sent.clear()
        paths = [f"~/Downloads/Gemini_Generated_Image_{i:02d}.png" for i in range(13)]
        replies[:] = [[f"scene number {i}" for i in range(8)], [f"scene number {i}" for i in range(8, 13)]]
        out = lib.execute_tool("name_images", {"paths": paths})
        ok &= check("13 captures (09-30): two vision calls, 13 moves", (len(sent), len(moves_of(out))), (2, 13))
        ok &= check("over 24: says to split", lib.execute_tool("name_images", {"paths": [f"~/{i}.png" for i in range(25)]})
                    .startswith("Error: at most 24 images"), True)

        print("\n── when vision doesn't answer right ──")
        replies[:] = [["only one name"]]
        out = lib.execute_tool("name_images", {"paths": ["~/a.png", "~/b.png"]})
        ok &= check("wrong count: no guesses, both skipped",
                    (moves_of(out), out.count("vision gave no usable name")), (None, 2))
        ok &= check("no paths: says what it needs", lib.execute_tool("name_images", {}).startswith("Error: paths is required"), True)
    finally:
        lib._gemini = orig_gem
        lib._TURN_IO.device = None

    print("\n── who can use it ──")
    ok &= check("needs the Mac", lib.execute_tool("name_images", {"paths": ["~/a.png"]}),
                "name_images needs Jellybean (the Mac bridge) for this turn.")
    ok &= check("adults only (it reads their Mac files)", sorted(set(hid.TOOL_ROLES.get("name_images", ())) - {"admin"}), ["adult"])
    ok &= check("advertised to the model", "name_images" in lib.build_tool_system_prompt(), True)
    return ok


if __name__ == "__main__":
    ok = run()
    print("\nAll tests passed." if ok else "\nSOME TESTS FAILED.")
    sys.exit(0 if ok else 1)
