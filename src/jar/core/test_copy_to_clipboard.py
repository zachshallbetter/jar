#!/usr/bin/env python3
# gate: hermetic
"""copy_to_clipboard (jellybean#212): text onto the clipboard of the device the person is asking from. A write (each
app's trust policy decides whether it asks), only the asking device itself (never the person's Mac from their phone,
never a shared household screen), plain text up to CLIPBOARD_MAX, and the clipboard is never read. Hermetic: a fake
device; nothing leaves the process.

Run: python3 scripts/test_copy_to_clipboard.py
"""
import os
import sys
import tempfile

os.environ.setdefault("AGENT_TURN_LOG", os.path.join(tempfile.mkdtemp(), "turns.jsonl"))
os.environ.setdefault("HOUSEHOLD_DB", os.path.join(tempfile.mkdtemp(), "household.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


spec = lib._DEVICE_TOOLS["copy_to_clipboard"]
print("── the tool ──")
check("a write, not a read (it replaces what the person had copied)", spec["risk"], "write")
check("only the asking device", spec.get("this_device"), True)
check("not phone-only (the Mac runs it too)", "copy_to_clipboard" in lib.PHONE_ONLY_TOOLS, False)
check("the iPhone runs it", "copy_to_clipboard" in lib.PHONE_DEVICE_TOOLS, True)
check("only a text parameter: nothing that reads", sorted(spec["parameters"]), ["text"])
check("never sent on to the person's Mac from their phone",
      ["copy_to_clipboard" in lib.mac_tools_for_phone(p) for p in (None, frozenset(), frozenset({"where_am_i"}))],
      [False, False, False])
check("people may; a shared household screen may not",
      [hh.role_allows_tool(r, "copy_to_clipboard") for r in ("admin", "adult", "kid", "household")], [True, True, True, False])

sent = []


def device(req):
    sent.append(req)
    return {"result": f"Copied {len(req['args']['text'])} characters to the clipboard."}


def turn(client, tools, role="adult", remote=None, dev=device):
    lib._TURN_IO.device, lib._TURN_IO.client, lib._TURN_IO.role = dev, client, role
    lib._TURN_IO.device_tools = tools
    lib._TURN_IO.remote = remote
    lib._TURN_IO.person = {"id": "p_alex", "name": "Alex"}


def done():
    lib._TURN_IO.device = lib._TURN_IO.client = lib._TURN_IO.role = lib._TURN_IO.device_tools = None
    lib._TURN_IO.remote = lib._TURN_IO.person = None


print("\n── on the asking device ──")
try:
    turn("iphone", lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS)))
    out = lib._execute_tool("copy_to_clipboard", {"text": "18 Alder St, Portland"})
    check("the phone gets it", (len(sent), sent[-1]["tool"], sent[-1]["args"], sent[-1]["risk"]),
          (1, "copy_to_clipboard", {"text": "18 Alder St, Portland"}, "write"))
    check("the device's own words come back", out, "Copied 21 characters to the clipboard.")
    check("offered on an iPhone turn", "copy_to_clipboard" in lib._turn_device_tools(), True)

    sent.clear()
    turn("mac", None)   # a Mac that reports nothing: the registry
    lib._execute_tool("copy_to_clipboard", {"text": "hello"})
    check("a Mac turn: the Mac gets it", [r["tool"] for r in sent], ["copy_to_clipboard"])

    print("\n── never anywhere else ──")
    sent.clear()
    older_phone = lib.device_tools_for_client("iphone", ["list_calendar_events", "where_am_i"])
    turn("iphone", older_phone, remote={"tools": lib.mac_tools_for_phone(older_phone), "from": "Alex's iPhone",
                                         "device": "Alex's Mac"})
    out = lib._execute_tool("copy_to_clipboard", {"text": "hello"})
    check("a phone without it: not sent to the person's Mac instead", sent, [])
    check("…and said plainly", out.startswith("Not done: copy_to_clipboard works only on the device you're asking from"), True)
    check("not offered on that phone", "copy_to_clipboard" in lib._turn_device_tools(), False)
    turn("iphone", older_phone, remote={"tools": frozenset({"copy_to_clipboard"}), "from": "x", "device": "y"})
    check("even if a remote list named it", lib._device_tool_allowed("copy_to_clipboard"), False)

    sent.clear()
    turn("mac", None, role="household")
    out = lib._execute_tool("copy_to_clipboard", {"text": "hello"})
    check("a household screen: refused, nothing sent", (sent, "copy_to_clipboard" in lib._turn_device_tools()), ([], False))

    print("\n── what can be copied ──")
    sent.clear()
    turn("iphone", lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS)))
    for args, why in (({"text": ""}, "empty"), ({"text": "   "}, "blank"), ({}, "missing"), ({"text": 42}, "not text"),
                      ({"text": ["a"]}, "a list"), ({"text": "x" * (lib.CLIPBOARD_MAX + 1)}, "too long")):
        out = lib._execute_tool("copy_to_clipboard", args)
        check(f"refused ({why}), nothing sent", (out.startswith("Not copied"), sent), (True, []))
    out = lib._execute_tool("copy_to_clipboard", {"text": "x" * lib.CLIPBOARD_MAX})
    check("exactly the most: sent", len(sent), 1)
    check("the most is 10,000 characters", lib.CLIPBOARD_MAX, 10_000)

    print("\n── no device, no pretending ──")
    sent.clear()
    turn(None, None, dev=None)
    out = lib._execute_tool("copy_to_clipboard", {"text": "hello"})
    check("no device in the turn: says it wasn't copied", (out.startswith("Not copied: no device"), sent), (True, []))
finally:
    done()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
