#!/usr/bin/env python3
"""What each of titan's own tools is, for the governance chokepoint (#1628 slice 2; design in
docs/design/governance-chokepoint.md).

Every tool in agent_runtime_lib's registry declares:
  domain      the trust domain it belongs to (a person's tier for that domain will apply, slice 3)
  risk        read, write or dangerous
  egress      the kinds of destination outside the house it sends data to (egress.KINDS), or none
  undo        how a change is reversed ("none" when it can't be); None for a read
  always_ask  true when it must ask every time, whatever the tier (it confirms on the person's own device today,
              or it can't be undone)

Who may use a tool (`roles`) still comes from household_identity's role table, and which clients reach it
(`surfaces`) from the Watch's tool list; `describe` folds both in. They move here in a later slice.

Nothing here changes what runs yet: test_tool_descriptors.py fails when a tool has no descriptor, a field is out of
vocabulary, or a descriptor disagrees with the rules titan enforces today.
"""
from __future__ import annotations

from dataclasses import dataclass

import household_identity

RISKS = ("read", "write", "dangerous")
SURFACES = ("mac", "iphone", "watch", "tv", "mcp", "background")

# titan's own domains, with the words a person sees in Settings (the Mac's domains are in agent_api_server's
# _DOMAIN_TEXT; `files` and `location` are shared vocabulary with the Mac).
DOMAINS: dict[str, str] = {
    "assistant": "how titan talks and works (dates, counting, choices, its own help)",
    "compute": "calculations and the code sandbox",
    "knowledgeBase": "the household knowledge base",
    "sharing": "sharing and private fences",
    "records": "records: bills, contacts, the timeline",
    "todos": "todos",
    "scheduling": "scheduled actions and routines",
    "tasks": "durable tasks titan works through",
    "home": "the house's devices",
    "homeSecurity": "locks, alarms, garage doors and cameras",
    "location": "where people are",
    "family": "custody and who's staying where",
    "mail": "mail on the mail servers",
    "web": "the web",
    "media": "images and speech",
    "notifications": "cards on someone's devices",
    "decisions": "decision models",
    "history": "the person's own Claude conversations and GitHub stars",
    "files": "files",
    "system": "titan's own system",
    "projects": "your projects: renaming and archiving them",
    "machines": "your other machines, paired as executors",
}


@dataclass(frozen=True)
class Descriptor:
    domain: str
    risk: str
    egress: tuple[str, ...] = ()
    undo: str | None = None
    always_ask: bool = False


def _r(domain: str, egress: tuple[str, ...] = ()) -> Descriptor:
    return Descriptor(domain, "read", egress)


def _w(domain: str, undo: str, egress: tuple[str, ...] = (), always_ask: bool = False) -> Descriptor:
    return Descriptor(domain, "write", egress, undo, always_ask)


def _d(domain: str, undo: str, egress: tuple[str, ...] = (), always_ask: bool = False) -> Descriptor:
    return Descriptor(domain, "dangerous", egress, undo, always_ask)


DESCRIPTORS: dict[str, Descriptor] = {
    # assistant
    "get_datetime": _r("assistant"),
    "count_chars": _r("assistant"),
    "tool_help": _r("assistant"),
    "skill_guide": _r("assistant"),
    "offer_choices": _r("assistant"),
    "set_preference": _w("assistant", "set_preference again"),
    # compute
    "python_eval": _r("compute"),
    "run_python": _r("compute"),   # a sandbox: no network, no files, 10 s
    # knowledge base
    "kb_retrieve": _r("knowledgeBase"),
    "kb_list": _r("knowledgeBase"),
    "kb_relations": _r("knowledgeBase"),
    "about": _r("knowledgeBase"),
    "situation": _r("knowledgeBase"),
    "threads": _r("knowledgeBase"),
    "open_item": _r("knowledgeBase"),
    "files": _r("files"),
    "ingest_text": _w("knowledgeBase", "kb_forget"),
    "ingest_url": _w("knowledgeBase", "kb_forget", egress=("fetch_url",)),
    "ingest_document": _w("knowledgeBase", "kb_forget", egress=("gemini",)),
    "kb_link": _w("knowledgeBase", "kb_link again"),
    "set_fact": _w("knowledgeBase", "set_fact again"),
    "kb_forget": _d("knowledgeBase", "none", always_ask=True),   # permanent
    # sharing
    "share": _w("sharing", "unshare"),
    "unshare": _w("sharing", "share"),
    "keep_private": _w("sharing", "keep_private again"),
    "list_private": _r("sharing"),
    # records
    "find_records": _r("records"),
    "recall_events": _r("records"),
    "open_obligations": _r("records"),
    "extract_bill": _w("records", "update_record", egress=("gemini",)),
    "log_event": _w("records", "update_record"),
    "update_record": _w("records", "update_record again"),
    "save_contact": _w("records", "update_record"),
    "mark_bills_paid": _w("records", "update_record"),
    # todos
    "add_todo": _w("todos", "complete_todo"),
    "list_todos": _r("todos"),
    "complete_todo": _w("todos", "add_todo"),
    "todo_later": _w("todos", "todo_later again"),                       # #1646: off the asker's own Now until a time
    # scheduling
    "schedule_action": _w("scheduling", "cancel_scheduled"),
    "list_scheduled": _r("scheduling"),
    "cancel_scheduled": _w("scheduling", "schedule_action"),
    "list_routines": _r("scheduling"),
    "save_routine": _w("scheduling", "save_routine again"),
    # tasks
    "plan_task": _w("tasks", "close_task"),
    "run_task": _w("tasks", "close_task"),
    "task_status": _r("tasks"),
    "task_answer": _w("tasks", "task_answer again"),
    "task_autonomy": _w("tasks", "task_autonomy off"),
    "close_task": _w("tasks", "plan_task"),
    "complete_step": _w("tasks", "none"),
    # home
    "home_status": _r("home"),
    "home_why": _r("home"),
    "home_control": _w("home", "home_control"),
    "lock_up": _d("homeSecurity", "none", always_ask=True),   # confirmed on the asker's own iPhone
    # location and family
    "where_is": _r("location"),
    "kids_staying": _w("family", "kids_staying again"),
    # mail
    "search_mail": _r("mail", egress=("mail",)),
    "mail_move_preview": _r("mail", egress=("mail",)),
    "mail_awaiting_reply": _r("mail"),                                   # #1644: titan's own store, nothing leaves
    "mail_move": _w("mail", "mail_move_undo", egress=("mail",), always_ask=True),   # a card on the owner's device
    "mail_move_undo": _w("mail", "mail_move", egress=("mail",), always_ask=True),
    "mail_unsubscribe_preview": _r("mail", egress=("mail",)),                # #1642: headers only, nothing sent
    "mail_unsubscribe": _w("mail", "none", egress=("mail", "mail-unsubscribe"), always_ask=True),   # a card; the tap POSTs once
    # web
    "web_search": _r("web", egress=("web_search", "gemini")),
    "fetch_url": _r("web", egress=("fetch_url",)),
    "get_weather": _r("web", egress=("weather",)),
    # media
    "view_image": _r("media", egress=("gemini",)),
    "describe_image": _r("media", egress=("gemini",)),
    "name_images": _r("media", egress=("gemini",)),
    "generate_image": _w("media", "none", egress=("gemini",)),
    "speak": _w("media", "none", egress=("gemini",)),   # Kokoro on titan; Gemini only as the fallback
    # notifications
    "notify_user": _w("notifications", "none", egress=("push",)),
    # decisions
    "jev_decide": _r("decisions", egress=("jev",)),
    "epistemic_query": _r("decisions"),
    # history
    "search_claude_chats": _r("history"),
    "github_stars": _r("history"),   # reads the daily sync's copy; the sync itself is the egress
    # titan's own system
    "host_status": _r("system"),
    "project_change": _w("projects", "project_change again"),
    "project_merge": _w("projects", "project_merge undo"),                # #1646: one undo token for the whole merge
    "list_projects": _r("projects"),                                      # #1646: names only, theirs and shared
    "project_items": _r("projects"),
    "project_split": _w("projects", "project_merge undo"),                # #1646: items of one's own project into a new one                                      # #1646: one project's page, as the asker sees it
    "what_i_did": _r("assistant"),                                       # #1645: the person's own record, counts only
    "machine_status": _r("machines"),                                    # #1631: one of the asker's own executors
    "titan_service": _r("system"),                                       # #1647: a service's state or its log
    "titan_service_restart": _w("system", "none", always_ask=True),     # #1647: asks every time (#1645 may loosen)
    "titan_gpu": _r("system"),                                           # #1647: each GPU's memory and processes
    "titan_health": _r("system"),                                        # #1647: a health check's last result
    "titan_health_run": _w("system", "none", always_ask=True),          # #1647: start the canary now; asks (#1645)
    "titan_resources": _r("system"),                                     # #1647: free disk and the named areas' sizes
    "titan_models": _r("system"),                                        # #1647: model servers and the GPUs they hold
    "titan_model_stop": _w("system", "titan_model_start", always_ask=True),   # #1647: code model or vision (Zach 10-14)
    "titan_model_start": _w("system", "titan_model_stop", always_ask=True),
    "titan_resources_sweep": _d("system", "none", always_ask=True),     # #1647: the agreed tmp sweep; removes files
    "capabilities": _r("system"),
    "recent_turns": _r("system"),
    "self_test": _r("system"),
    "run_check": _w("system", "none"),   # runs a named check on the person's Mac
    "check_result": _r("system"),
    "weekly_review": _r("system"),
    "review_decide": _w("system", "review_decide again"),
    "file_list": _r("system"),
    "file_read": _r("system"),
    "file_write": _d("system", "none"),
    "file_edit": _d("system", "none"),
}


# always_ask tools whose own code never acts in the model's call: the tool only sends a card, and only the owner's tap on
# their own device acts (lock_up: home_security.request, then a tap on the adult's iPhone; mail_move / mail_move_undo:
# mail_moves.request, then a tap on their iPhone or Mac). test_always_ask proves each one. Every other always_ask tool
# (kb_forget, a connected server's destructive tools) is held by the runtime until the person taps Do it (#1628).
CONFIRMS_ON_DEVICE = frozenset({"lock_up", "mail_move", "mail_move_undo", "mail_unsubscribe"})


def roles(tool: str) -> frozenset[str]:
    """Who may use a tool, from household_identity's role table (anything not listed is the admin's only)."""
    return frozenset({"admin"}) | household_identity.TOOL_ROLES.get(tool, frozenset())


def surfaces(tool: str) -> frozenset[str]:
    """Which clients reach a tool: every surface, and the Watch only for its own short list."""
    every = frozenset(s for s in SURFACES if s != "watch")
    return every | ({"watch"} if tool in household_identity.WATCH_TOOLS else frozenset())


def lookup(tool: str) -> Descriptor | None:
    """A tool's Descriptor: titan's own, or a connected MCP server's (mcp_host, #1629); None when it has none."""
    d = DESCRIPTORS.get(tool)
    if d is None and tool.startswith("mcp_"):
        import mcp_host
        d = mcp_host.descriptor(tool)
    return d


def describe(tool: str) -> dict | None:
    """A tool's whole descriptor, roles and surfaces included; None when it has none."""
    d = DESCRIPTORS.get(tool)
    if d is None:
        return None
    return {"id": f"titan:{tool}", "domain": d.domain, "risk": d.risk, "egress": list(d.egress), "undo": d.undo,
            "always_ask": d.always_ask, "roles": sorted(roles(tool)), "surfaces": sorted(surfaces(tool))}
