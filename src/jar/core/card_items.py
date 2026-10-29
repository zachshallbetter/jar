#!/usr/bin/env python3
"""Cards carry their item (#1235, the #1088 card Open slice): a proactive card about an item the viewer may see gets
`item: {id, project?}`, so a card's Open goes to the item in its project.

  per viewer, per serve   decided each time cards are served, from this viewer's own item readers (the same readers and
                          the same viewer rule as GET /v1/items?view=now: owner, audience, kid/parent, a shared screen
                          the household's items only). Never stored on the card: one card shown to two viewers can carry
                          `item` for one and not the other.
  only a known link       a card is about an item only when its source and key name one (below); the item must be in
                          what this viewer's reader returns, exactly once. Anything else, or any lookup that fails,
                          leaves `item` off. Never guessed from the card's text.
  project                 only from that viewer-visible item's own projects, and only when exactly one of them is a
                          project (or group) this viewer can see. Otherwise left off; the client falls back to Now's row.
  never on safety cards   Lock Up and alarms (proactive.SAFETY_SOURCES), nor an item its adapter marks `safety`.
  cheap                   only the readers the served cards need, each once per request (not once per card); a list
                          with no linkable card reads no store at all.

Which cards name an item (source, key):
  scheduled          the action's own card key (sched-<id>, kept by every run of a repeating series) → scheduled:<id>
  tasks              task-<id> (task_runner)                                           → task:<id>
  routine            place-<id> (a routine's place card)                               → routine:<id>
  file-share-changed file-changed-<ref id> (data.ref_id)                               → ref:<id>
  bill-due           bill-due:<key> (bill_due_cards: account and due date)             → that bill's thread
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

SOURCES = ("scheduled", "tasks", "routine", "file-share-changed", "bill-due")   # the sources whose cards name an item
Reader = Callable[[], list[dict[str, Any]]]
Ref = tuple[str, Callable[[dict[str, Any]], bool]]   # (reader name, does this item match)


def _after(key: str, prefix: str) -> str | None:
    return (key[len(prefix):] or None) if key.startswith(prefix) else None


def bill_due_row(key: str) -> tuple[str, str] | None:
    """(account, due ISO date) a bill-due card's key stands for (bill_due.bill_due_cards), else None."""
    import bill_due
    con = bill_due._db()
    try:
        r = con.execute("SELECT account, due FROM bill_due_cards WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    return (str(r["account"]), str(r["due"])[:10]) if r else None


def ref(card: dict[str, Any], bill_row: Callable[[str], tuple[str, str] | None] | None = None) -> Ref | None:
    """Which reader holds the item this card is about, and how to know it there. None: not about an item."""
    src, key = str(card.get("source") or ""), str(card.get("key") or "")
    import proactive
    if not key or proactive.is_safety_source(src):
        return None
    if src == "scheduled":
        # a repeating series keeps its first row's card key on every later row: match the row's own key
        return "scheduled", lambda it: it.get("card_key") == key
    if src == "tasks" and (tid := _after(key, "task-")):
        return "tasks", lambda it: it["id"] == f"task:{tid}"
    if src == "routine" and (rid := _after(key, "place-")):
        return "routines", lambda it: it["id"] == f"routine:{rid}"
    if src == "file-share-changed" and (rid := _after(key, "file-changed-")):
        data = card.get("data") or {}
        if isinstance(data, dict) and data.get("ref_id") not in (None, rid):
            return None   # the card's two names for its file disagree: no link
        return "references", lambda it: it["id"] == f"ref:{rid}"
    if src == "bill-due" and (k := _after(key, "bill-due:")):
        row = (bill_row or bill_due_row)(k)
        if not row:
            return None
        account, due = row
        return "bills", lambda it: (it["id"].rsplit(":", 1)[0] == f"bill:{account}"
                                    and str((it.get("time") or {}).get("due") or "")[:10] == due)
    return None


def attach(cards: list[dict[str, Any]], readers: dict[str, Reader],
           project_ok: Callable[[str], bool] | None = None,
           bill_row: Callable[[str], tuple[str, str] | None] | None = None) -> list[dict[str, Any]]:
    """Set `item` on each card that names an item this viewer's `readers` return (exactly one match), and drop any
    `item` already there. `readers` are _item_readers bound to the viewer; `project_ok(p)`: may this viewer see p.
    Changes the cards in place and returns them."""
    wants: dict[int, Ref] = {}
    for i, c in enumerate(cards):
        c.pop("item", None)
        try:
            r = ref(c, bill_row)
        except Exception:  # noqa: BLE001 - an unreadable lookup: no link, never a guess
            r = None
        if r is not None and r[0] in readers:
            wants[i] = r
    if not wants:
        return cards
    names = sorted({r[0] for r in wants.values()})
    got: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(len(names), 6))) as pool:
        futures = {n: pool.submit(readers[n]) for n in names}
    for n, f in futures.items():
        try:
            got[n] = f.result()
        except Exception:  # noqa: BLE001 - a store down: its cards carry no item
            continue
    seen: dict[str, bool] = {}

    def visible(p: str) -> bool:
        if p not in seen:
            try:
                seen[p] = bool(project_ok and project_ok(p))
            except Exception:  # noqa: BLE001 - unknown is not visible
                seen[p] = False
        return seen[p]
    for i, (name, match) in wants.items():
        hits = [it for it in got.get(name, ()) if match(it)]
        if len(hits) != 1 or hits[0].get("safety"):
            continue
        it = hits[0]
        link: dict[str, Any] = {"id": it["id"]}
        projs = sorted({p for p in it.get("projects") or [] if visible(p)})
        if len(projs) == 1:
            link["project"] = projs[0]
        cards[i]["item"] = link
    return cards
