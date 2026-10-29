#!/usr/bin/env python3
"""Kept bundles, and the facet and project each scan files under (titan-node#1505, epic #1498 slice 6).

A bundle is a suggested kept collection of one person's documents, a set of REFERENCES (scan ids, which the device holds,
#446), never copies and never text:
  tax-<year>                 W-2s, 1099s, 1098s, donation and deductible receipts that name the tax year
  policy:<sender>            one insurer's policy pages and declarations
  warranty:<sender>          one maker's warranty certificates
  house                      the mortgage, its utilities (power, water, broadband), warranties and home insurance
  vehicle                    the car's insurance and the shop that services it
The first three are what a document's association is (the benchmark's `assoc` bundle); house and vehicle are second
memberships (a mortgage bill stays on its bill cycle and is also in "the house"). A bundle is derived from the documents each
time, so there is no store of its own and the same pass again is a no-op.

Facet and project, through the machinery that already files things (not a parallel store):
  facet    one of household_identity.FACETS (personal, social, work, school) or "home" (the household's, as world.py files a
           shared bill). The owner's OWN facet map ("vendor:<name>" -> facet, the map items.bills_now reads) decides first; no
           map, no entry: the vendor classifier here (words in the sender, and the kind of document) guesses. Junk files
           nowhere. A guess, which the owner confirms or changes (set_facets).
  project  a bundle that holds projects.MIN_ITEMS documents is PROPOSED as a project, named with projects.short_name, in the
           shape projects.proposals makes ({id, name, name_source, count, items, reason}); the id is threadview.thread_id of
           its items. Nothing is created until the owner confirms (confirm(): spaces.create_project, private, then the
           projects filings, one undo token). A bundle mostly filed already, or one the owner dismissed, proposes nothing.

Privacy, absolute, and filing only:
  - a bundle has ONE owner, the scanner (never a printed name, #1428), and its audience is [owner]. Bundles are built one
    owner at a time, so two people's tax forms for the same year are two bundles that share a key and nothing else.
  - a facet or a project is filing words. This stage never changes who owns a scan, what a viewer's rows hold, or an audience; only
    the OWNER's facet map applies to the owner's documents, another person's map never reaches them.
  - a bundle is shown to its owner alone: bundle_for(bundle, viewer) is None for anyone else, whole, never a part, a count or a total.
  - nothing a scan says is acted on; the text is only read for a tax year, a policy kind and the words above.

  python3 scripts/scans_score.py --pipeline scan_bundles:pipeline
  python3 scripts/scans_score.py --pipeline scan_bundles:pipeline --write scripts/evalsets/scans/bundles.json
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402

VERSION = 1
KEPT_TYPES = ("tax_form", "policy", "warranty")          # the types whose primary association is a bundle
PROJECT_KEYS = re.compile(r"^(?:house|vehicle|tax-\d{4})$")   # the collections big enough to be a project
_YEAR = re.compile(r"(?i)\btax year\s*[:#-]?\s*(20\d\d)\b")
_FORM_YEAR = re.compile(r"(?i)\bform\s+(?:w-?2|w-?2g|1099(?:-[a-z]+)?|1098(?:-[a-z]+)?|5498)\b[^\n]*?\b(20\d\d)\b")
_TITLE_YEAR = re.compile(r"(?i)\b(20\d\d)\b[^\n]*\bfor tax purposes\b|\bfor tax purposes\b[^\n]*\b(20\d\d)\b")
_HOUSE_SENDER = re.compile(r"(?i)\b(mortgage|power|electric|gas|water|sewer|broadband|internet|utilit\w*)\b")
_HOUSE_TEXT = re.compile(r"(?i)\b(homeowners?|dwelling|home insurance)\b")
_VEHICLE_SENDER = re.compile(r"(?i)\b(auto|automotive|automobile|motors?|vehicle|dmv)\b")
# the vendor classifier: the first rule that matches the sender's words wins; no match is personal (what world.py files an
# unmapped private bill under). An insurer that is not an auto insurer is the household's.
_FACET_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("work", re.compile(r"(?i)\b(staffing|payroll|employer|consulting)\b")),
    ("school", re.compile(r"(?i)\b(school|elementary|college|university|academy)\b")),
    ("social", re.compile(r"(?i)\b(library|parks|recreation|neighborhood|association|radio|pantry|shelter)\b")),
    ("personal", re.compile(r"(?i)\b(auto|automotive|motors?|clinic|dental|pharmacy|lab|hospital|fitness|student|card|mobile|savings)\b")),
    ("home", re.compile(r"(?i)\b(mortgage|joint|power|electric|gas|water|sewer|broadband|internet|appliances|electronics|"
                        r"property|county|insurance|utilit\w*)\b")),
)
_NAMES = {"house": "The house", "vehicle": "The car"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def valid_facets() -> tuple[str, ...]:
    import household_identity as hh
    return tuple(hh.FACETS) + ("home",)


def facet_of(owner: str, sender: str | None, typ: str, facets: dict[str, dict[str, str]] | None = None) -> str | None:
    """The facet a document files under. The OWNER's own map first ("vendor:<name>", lowercased, the map items.bills_now
    reads; another person's map is never looked at), else the classifier's guess. Junk files nowhere."""
    if typ == "junk":
        return None
    name = " ".join(str(sender or "").split()).lower()
    mapped = ((facets or {}).get(owner) or {}).get(f"vendor:{name}") if name else None
    if mapped in valid_facets():
        return mapped
    for facet, rx in _FACET_RULES:
        if name and rx.search(name):
            return facet
    return "personal"


def tax_year(texts: list[str]) -> str | None:
    """The year a tax document names: 'Tax year 2025', 'Form W-2 ... 2025', '... 2025 for tax purposes'. The most common answer
    among the document's copies (an OCR slip in one copy is outvoted); none when no copy names a year."""
    years: Counter = Counter()
    for t in texts:
        for rx in (_YEAR, _FORM_YEAR, _TITLE_YEAR):
            m = rx.search(t)
            if m:
                years[next(g for g in m.groups() if g)] += 1
                break
    return years.most_common(1)[0][0] if years else None


def collections_of(typ: str, sender: str | None, texts: list[str]) -> list[str]:
    """Every bundle key a document belongs to."""
    if typ == "junk":
        return []
    out: set[str] = set()
    name = sender or ""
    if typ == "tax_form":
        y = tax_year(texts)
        if y:
            out.add(f"tax-{y}")
    elif typ in ("policy", "warranty") and name:
        out.add(f"{typ}:{_slug(name)}")
    body = "\n".join(texts)
    if _HOUSE_SENDER.search(name) or typ == "warranty" or (typ == "policy" and _HOUSE_TEXT.search(body)):
        out.add("house")
    if _VEHICLE_SENDER.search(name) and not _HOUSE_SENDER.search(name):
        out.add("vehicle")
    return sorted(out)


def _name_of(key: str) -> str:
    if key in _NAMES:
        return _NAMES[key]
    m = re.match(r"tax-(\d{4})$", key)
    return f"Tax {m.group(1)}" if m else key


def bundles(scans: list[dict], docs: dict[str, dict], *, facets: dict[str, dict[str, str]] | None = None,
            filed: dict[str, str] | None = None, dismissed: set[str] | None = None) -> dict[str, Any]:
    """The bundles over the batch, built one owner at a time. `docs` is a prediction's docs (owner, cluster, type, sender,
    amount). `facets` is {owner: {"vendor:<name>": facet}}, `filed` is projects.filings() ({item: project}), `dismissed` the
    proposal ids the owner said no to. Returns {"bundles": [...], "of": {scan id: [keys]}, "facet": {...}, "projects": {...}}."""
    import projects
    import threadview
    by_id = {s["id"]: s for s in scans}
    members: dict[tuple[str, str], list[str]] = defaultdict(list)           # one document = one owner's dedupe cluster
    for sid, d in sorted(docs.items()):
        members[(d["owner"], d.get("cluster") or sid)].append(sid)
    facet: dict[str, str | None] = {}
    inside: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)   # (owner, key) -> the documents in it
    for (owner, cluster), sids in sorted(members.items()):
        first = docs[sids[0]]
        texts = [by_id[m]["text"] for m in sids]
        f = facet_of(owner, first.get("sender"), first.get("type") or "", facets)
        for m in sids:
            facet[m] = f
        for key in collections_of(first.get("type") or "", first.get("sender"), texts):
            inside[(owner, key)].append((owner, cluster))
    out: list[dict[str, Any]] = []
    of: dict[str, list[str]] = defaultdict(list)
    proj_of: dict[str, list[str]] = defaultdict(list)
    for (owner, key), cl in sorted(inside.items()):
        scan_ids = sorted(m for o, c in cl for m in members[(o, c)])
        if any(docs[m]["owner"] != owner for m in scan_ids):
            raise AssertionError("a bundle holds two owners")
        items = [f"scan:{c}" for _, c in cl]
        fac = Counter(f for f in (facet[m] for m in scan_ids) if f)
        b: dict[str, Any] = {"id": "bd-" + hashlib.sha1(f"{owner}\0{key}".encode()).hexdigest()[:12], "key": key,
                             "name": projects.short_name(_name_of(key)), "owner": owner, "audience": [owner],
                             "documents": [c for _, c in cl], "scans": scan_ids, "count": len(cl),
                             "facet": sorted(fac.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if fac else None,
                             "project": None, "state": "proposed"}
        fv = filed or {}
        mostly = sum(1 for i in items if i in fv) * 2 >= len(items)
        if PROJECT_KEYS.match(key) and len(items) >= projects.MIN_ITEMS and not mostly:
            pid = threadview.thread_id(items)
            if pid not in (dismissed or set()):
                b["project"] = {"id": pid, "key": f"project:{key}", "name": b["name"], "name_source": "description",
                                "count": len(items), "items": items,
                                "reason": {"key": "projects.proposal.holds_together", "count": len(items)}}
        out.append(b)
        for m in scan_ids:
            of[m].append(key)
            if b["project"]:
                proj_of[m].append(b["project"]["key"])
    return {"bundles": out, "of": dict(of), "facet": facet, "projects": dict(proj_of)}


def bundle_for(bundle: dict[str, Any], viewer: str) -> dict[str, Any] | None:
    """A bundle as a viewer may have it: whole to its owner, nothing to anyone else (no part, no count, no total)."""
    return bundle if viewer and viewer == bundle["owner"] else None


def confirm(bundle: dict[str, Any], person: str, role: str, *, name: Any = None, db: Path | None = None) -> dict[str, Any]:
    """The owner keeps the proposed project: a private project is made and the bundle's documents are filed in it (projects
    filings, items 'scan:<cluster>'), one undo token. Anyone but the owner gets a 404, as for any project they cannot see."""
    import projects
    import spaces
    p = bundle.get("project")
    if not p or not person or person.lower() != bundle["owner"]:
        raise projects.Refused(404, "no such proposal")
    proj = spaces.create_project(person, role, name or p["name"], audience="private", db=db)
    token = projects._file_many(person, role, p["items"], proj["id"], db)
    return {"project": proj["id"], "items": len(p["items"]), "undo": token}


def pipeline(scans: list[dict]) -> dict:
    """The scorer's pipeline: dedupe's prediction, then bundles. Bundles alone, the bundles of the stages before it are
    not needed (see scan_pipeline for the whole chain)."""
    return stage(scans, D.pipeline(scans))


def stage(scans: list[dict], pred: dict, *, facets: dict[str, dict[str, str]] | None = None,
          filed: dict[str, str] | None = None, dismissed: set[str] | None = None) -> dict:
    """Bundles as a stage: adds `bundles`, a `facet` and `projects` to every document, and sets the association of a tax form,
    a policy or a warranty to its bundle (unless an earlier stage gave it another). Rows, views, owners and waiting are not touched."""
    res = bundles(scans, pred["docs"], facets=facets, filed=filed, dismissed=dismissed)
    for sid, d in pred["docs"].items():
        d["bundles"] = res["of"].get(sid, [])
        d["facet"] = res["facet"].get(sid)
        d["projects"] = res["projects"].get(sid, [])
        primary = [k for k in d["bundles"] if k.startswith(("tax-", "policy:", "warranty:"))]
        if d.get("type") in KEPT_TYPES and primary and (d.get("assoc") or {}).get("kind") in (None, "none", "bundle"):
            d["assoc"] = {"kind": "bundle", "key": primary[0]}
    pred["bundles"] = res["bundles"]
    return pred
