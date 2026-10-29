#!/usr/bin/env python3
"""Household identity: who is asking titan, from which device, and what they may do.

Every client used to paste the one master agent token. Now each device gets its own revocable
token tied to a person, and each person has a role (admin, adult, kid, household). The contract
the clients build against is in docs/runbook.md › Household identity and pairing.

Storage: SQLite at HOUSEHOLD_DB (default /ai/datasets/connectors/household.db):
  people       id, name, role
  devices      id, person_id, name, platform, token_sha256 (never the token), created_at,
               last_seen, revoked_at
  pair_codes   6-digit codes: single use, 10 minutes, at most 5 open at once

Pairing: an admin starts a pairing for a person (POST /v1/pair/start) and gets a code + QR; the
new device claims it without auth (POST /v1/pair/claim) and receives its token once. Claims are
rate limited per client IP and globally (a burst of failed guesses locks every claim out for a
while), which with at most 5 open codes out of a million makes guessing hopeless.

The master token (/ai/config/agent_api_token.txt) keeps working and always means Zach (admin), so
the Mac and scripts don't change.

What each role may do (tools, house domains, API routes, the `can` map) is ROLE POLICY below:
one table, read by agent_runtime_lib (tools), home_assistant (domains) and agent_api_server
(routes and /v1/me).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import json
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from ops_ports import AGENT_API_PORT

ROLES = ("admin", "adult", "kid", "household")
# Not a person's role: the credential an engineering agent (a Claude session, the autonomous coder) reads titan
# with (#355). System notes and titan's host summary only, read-only, through a few GET routes; never a household
# item. The token lives in a file (AGENT_ENGINEERING_TOKEN_FILE); without one, nobody resolves to it.
ENGINEERING = "engineering"
PLATFORMS = ("ios", "ipados", "mac", "tvos", "watchos", "other")   # tvos: the Apple TV as a household device (jellybean#183)
ADMIN_PERSON_ID = "zach"          # the seeded admin; the master token resolves to this person
ADMIN_PERSON_NAME = "Zach"

CODE_TTL_S = 600                  # a pairing code lives 10 minutes
MAX_OPEN_CODES = 5
CLAIMS_PER_IP_PER_MIN = 10
GLOBAL_FAILURES_PER_HOUR = 30     # this many failed claims in an hour → every claim 429 for LOCKOUT_S
LOCKOUT_S = 15 * 60
LAST_SEEN_EVERY_S = 60            # a device's last_seen is written at most once a minute
REFUSAL = "That's not something I can do for you."

# The URLs a paired device tries, in order, when titan can't work them out (TITAN_PAIR_URLS
# overrides both: comma-separated).
DEFAULT_SERVER_URLS = (f"http://10.0.0.170:{AGENT_API_PORT}", f"http://100.77.69.85:{AGENT_API_PORT}",
                       f"http://titan-node.ladon-betelgeuse.ts.net:{AGENT_API_PORT}")


# ════════════════════════════════════════════════════════════════════════════════════════════
# ROLE POLICY — the one table. admin can use everything; the columns say what the other roles
# may use. A tool that isn't listed here is admin-only (so a new tool is never handed to a kid
# by accident; test_household_identity checks every registered tool is listed).
#
#   adult      full access to their own personal things (their memory, their device's calendar,
#              reminders, notes, messages, files and automation on their own device), the house,
#              their own knowledge-base notes plus what's shared with them (kb_privacy: they edit and
#              share their own), records and the household timeline.
#   kid        their own memory and their own device's calendar/reminders/alarms, chat and
#              homework help (compute, web search), the knowledge base's household items (read), the house
#              within KID_HOME_DOMAINS. Never adults' personal data, files, shell, locks/cameras.
#   household  a shared device, not a person: house control, the family calendar on that device,
#              weather, routines (run and edit), the knowledge base's household items (read). No personal
#              memory, messages, notes, mail, files, contacts.
#
# Admin-only (no column): titan's own files and ops (file_*, host_status, run_check,
# check_result, self_test, jev_decide), host-file readers (describe_image, extract_bill,
# ingest_document), Zach's Mac cards and plan card (notify_user, plan_task, run_task,
# task_status, complete_step, close_task), and the admin's own GitHub stars (github_stars).
# Device tools run on the asking device itself.
# ════════════════════════════════════════════════════════════════════════════════════════════
_ROLE_TOOL_TABLE = """
# tool                    adult kid household
get_datetime                y    y    y
count_chars                 y    y    y
python_eval                 y    y    y
run_python                  y    -    -      # Zach 10-14: adults only, a role rule (never a setting anyone flips)
web_search                  y    y    y
fetch_url                   y    -    -
epistemic_query             y    -    -
get_weather                 y    y    y
where_is                    y    y    -      # own place, a parent's kids' (#423, Zach 10-02); never on a household device
kids_staying                y    -    -      # custody (custody.py): a kid's parents set where the kids are staying; kids read it in context
home_status                 y    y    y      # kid, household: locks/cameras/alarms hidden
home_why                    y    y    y      # household-visible devices only; never locks/alarms/garage/cameras/location
home_control                y    y    y      # kid: KID_HOME_DOMAINS only
lock_up                     y    -    -      # #413: lock only, confirmed on the asker's own iPhone; HA_LOCK_UP=1
list_routines               y    y    y
save_routine                y    -    y
kb_retrieve                 y    y    y      # each sees only the KB items their audience allows;
kb_list                     y    y    y      # kid and household: household items only (kb_privacy)
kb_relations                y    y    y
about                       y    y    y      # world model (#325): each sees only claims their evidence allows
situation                   y    y    y      # FE graph (#1399, AGENT_FE_GRAPH): each sees only their visible nodes/edges
keep_private                y    -    -      # fences (#356): adults and the admin set them, kids don't
set_fact                    y    y    -      # world model (#358): adults; parents for their kids' schedules; a kid's statement is proposed until a parent confirms
list_private                y    -    -
ingest_text                 y    -    -
ingest_url                  y    -    -
kb_link                     y    -    -
kb_forget                   y    -    -      # own notes only (kb_privacy.can_edit)
share                       y    -    -      # widen who sees an own KB note (kb_privacy.share)
unshare                     y    -    -
log_event                   y    -    -
recall_events               y    -    -
find_records                y    -    -
open_obligations            y    -    -
mark_bills_paid             y    -    -      # #567: the person's own word that their bills are handled
threads                     y    -    -
update_record               y    -    -
save_contact                y    -    -
view_image                  y    y    -
name_images                 y    -    -      # proposes names; move_files renames
search_claude_chats         y    -    -      # the asker's own Claude conversations only
search_mail                 y    -    -      # #955: the asker's own mailboxes only, on their servers
mail_move_preview           y    -    -      # #955 step 5: own inbox only, nothing moves
mail_awaiting_reply         y    -    -      # #1644: own inbox, read-only, who's waiting on a reply
project_split               y    -    -      # #1646: their own project's items, named as their page shows them
project_items               y    y    -      # #1646: one project's page, exactly as the asker's own app shows it
list_projects               y    y    -      # #1646: their own and shared, names only (a kid's projects too)
project_merge               y    -    -      # #1646: two of their own projects, one undo token
project_change              y    -    -      # #1646: rename or archive their own projects, never their audience
mail_move                   y    -    -      # asks on the owner's own iPhone or Mac; the tap moves
mail_move_undo              y    -    -
mail_unsubscribe_preview    y    -    -      # #1642: own inbox, headers only
mail_unsubscribe            y    -    -      # asks on the owner's own iPhone or Mac; the tap sends one POST
generate_image              y    -    -
speak                       y    -    -
set_preference              y    y    y      # the asking person's own (PREFERENCES)
add_todo                    y    y    y      # own and household lists (todos.py); a kid only those
list_todos                  y    y    y
complete_todo               y    y    y
todo_later                  y    y    -      # #1646: off the asker's own Now; a shared screen has no Now of its own
schedule_action             y    y    -      # the asker's own, run later as them (scheduled.py); a kid's
list_scheduled              y    y    -      # only with a kid's tools. Not a shared device: an action
cancel_scheduled            y    y    -      # needs a person to own it and see its card
notify_user                 y    y    y      # cards to people; who may send to whom: card_send_refusal
skill_guide                 y    -    -      # the Agent Skills guides (they describe adults' tools)
tool_help                   y    y    y      # read-only; describes only tools the asker's role may use
# device tools (run on the asking device, which asks its user as it always has)
travel_time                 y    y    y      # read-only, from the asking iPhone's location
play_music                  y    y    y      # the asking iPhone's own Music app
music_control               y    y    y
now_playing                 y    y    y
health_summary              y    y    y      # the asker's own Apple Health, on their own iPhone
where_am_i                  y    y    -      # the asker's own iPhone, where it is now (jellybean#387); not a shared device
photos_find                 y    y    -      # the asker's own Photos library, counts only (jellybean#188); not a shared device
copy_to_clipboard           y    y    -      # onto the asker's own device's clipboard (jellybean#212); never a shared screen
list_calendar_events        y    y    y
create_calendar_event       y    y    y
update_calendar_event       y    y    y      # edits run on the asking person's own device, with Undo
delete_calendar_event       y    y    y
list_reminders              y    y    y
create_reminder             y    y    y
complete_reminder           y    y    y
update_reminder             y    y    y
delete_reminder             y    y    y
set_alarm                   y    y    y
fetch_image                 y    y    -      # internal, follows view_image
search_contacts             y    y    -
create_contact              y    -    -
send_message                y    -    -
search_messages             y    -    -      # the asking adult's own texts (Full Disk Access on their Mac)
find_my_locate              y    -    -      # where someone is: the asking adult's own Find My, on their Mac
offer_choices               y    y    y      # the question titan asks, as tappable options (runs nothing)
open_item                   y    y    -      # #446 step 3: a synced mail or scan's body from the owner's device, this once (owner's yes for anyone else)
draft_email                 y    -    -
mail_cleanup_preview        y    -    -      # jellybean#211: old mail by sender on the asker's Mac (headers only)
mail_archive                y    -    -      # jellybean#211: to Archive, never deleted; the Mac asks every time; Undo
search_notes                y    -    -
read_file                   y    -    -
write_file                  y    -    -
move_files                  y    -    -      # Mac moves, checked, one Undo (Jellybean 0.12.2+)
trash_files                 y    -    -      # #836: Mac deletes are moves to the Trash; asks every time; checked; one Undo (0.13.34+)
list_files                  y    -    -
find_files                  y    -    -
search_file_contents        y    -    -
files                       y    -    -      # #1251 C: the three above as modes; each call is checked as its mode's own tool
run_shell                   y    -    -
run_applescript             y    -    -
run_shortcut                y    -    -
create_skill                y    -    -
run_skill                   y    -    -
list_skills                 y    -    -
ax_click                    y    -    -
ax_focus                    y    -    -
ax_read                     y    -    -
ax_type                     y    -    -
mouse_click                 y    -    -
get_screen                  y    -    -
get_field_state             y    -    -
get_running_apps            y    -    -
what_i_did                  y    y    -      # #1645: the asker's own record, counts only (a kid's is theirs too)
machine_status              y    -    -      # #1631: the asker's own paired machines; never a kid's or a screen's
check_app                   y    -    -      # titan-node#1638: one Mac app's version, state and crash reports
get_frontmost_app           y    -    -
get_browser_url             y    -    -
get_finder_path             y    -    -
git_status                  y    -    -
list_listening_ports        y    -    -
send_to_claude              y    -    -
run_agent                   y    -    -      # jellybean#209: Claude in a new worktree on the asker's Mac (asks every time there)
agent_status                y    -    -
agent_discard               y    -    -
undo_last_action            y    -    -
"""

# How a person wants titan to talk to them: name → (default, what "on" means). Set by the person in
# chat (set_preference), never by a knowledge-base note (those can't be rules, #158).
PREFERENCES: dict[str, tuple[str, str]] = {
    "ask_first": ("off", "ask in words before making any change, even a clear one"),
    "offers": ("off", "end replies with an offer of a next step (\"Want me to…?\") when one fits"),
    # A phone setting, not how titan talks (never in the turn's [PREFERENCES] line): a task's Live Activity always
    # shows its goal (Zach, 10-03); this adds the current step's title and what it waits on (#725). Set from the app
    # (PATCH /v1/me).
    "live_activity_detail": ("off", "show a task's current step and what it waits on on the Lock Screen"),
}
DISPLAY_PREFERENCES = frozenset({"live_activity_detail"})


# Tools that exist but are admin-only (listed so the "every tool is in the table" test can tell
# a deliberate admin-only tool from a forgotten one).
ADMIN_ONLY_TOOLS = frozenset({
    "file_read", "file_write", "file_edit", "file_list", "host_status", "capabilities", "run_check", "check_result",
    "self_test", "jev_decide", "describe_image", "extract_bill", "ingest_document",
    "plan_task", "run_task", "task_status", "complete_step", "close_task",
    "weekly_review", "review_decide", "recent_turns",   # titan's own review of real turns (#349)
    "task_autonomy", "task_answer",                      # tasks that advance on their own (#343)
    "github_stars",                                      # the admin's own GitHub stars (#707)
    "titan_service", "titan_service_restart", "titan_gpu",   # titan acting on itself (#1647)
    "titan_health", "titan_health_run", "titan_resources", "titan_resources_sweep", "titan_models",
    "titan_model_stop", "titan_model_start",
})

# Home Assistant domains a kid may change (the thermostat within home_assistant's safe range).
# household and adult keep home_assistant.CONTROLLABLE; nobody changes locks/alarms/cameras.
KID_HOME_DOMAINS = frozenset({"light", "switch", "media_player", "scene", "climate"})
# What a Watch token (via "watch", a kid's Watch paired directly, #755) may run, whatever its role allows elsewhere
# (10-07 review W3): enforced where every tool call runs and in what's advertised, not only in the prompt. Short,
# answerable on a wrist, nothing that reads anyone's words or acts on a device titan would have to wake. A tool joins
# only by a deliberate edit here (test_kid_switch checks each is also in the kid column).
WATCH_TOOLS = frozenset({
    "get_datetime", "count_chars", "get_weather", "home_status", "home_why", "home_control", "list_routines",
    "kb_retrieve", "about", "list_todos", "add_todo", "complete_todo", "tool_help",
})
# Roles that don't see locks, alarms, garage doors or cameras in home_status.
SECURITY_HIDDEN_ROLES = frozenset({"kid", "household"})


def _parse_tool_table(text: str) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    cols = ("adult", "kid", "household")
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, *marks = line.split()
        if len(marks) != 3 or any(m not in ("y", "-") for m in marks):
            raise ValueError(f"bad role table row: {line!r}")
        out[name] = frozenset({"admin"} | {c for c, m in zip(cols, marks) if m == "y"})
    return out


TOOL_ROLES: dict[str, frozenset[str]] = _parse_tool_table(_ROLE_TOOL_TABLE)


def role_allows_tool(role: str | None, tool: str) -> bool:
    """Whether a role may see and run a tool. None (the CLI, background turns) is admin."""
    if not role or role == "admin":
        return True
    return role in TOOL_ROLES.get(tool, frozenset({"admin"}))


def home_domains_for(role: str | None) -> frozenset[str] | None:
    """The Home Assistant domains a role may change (None: home_assistant's own list)."""
    return KID_HOME_DOMAINS if role == "kid" else None


# A kid's Watch paired directly (titan-node#755): how long its token lives, and how many refused confirmations a
# pairing code takes before it's spent.
WATCH_IDLE_S = 90 * 86400
WATCH_MAX_S = 365 * 86400
WATCH_CONFIRM_ATTEMPTS = 3
# The only routes a watch token (via "watch") reaches, whatever its role may call elsewhere; checked before ROUTE_ROLES.
WATCH_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((m, re.compile(p)) for m, p in (
    ("GET", r"/v1/me"),
    ("GET", r"/v1/watch/today"),
    ("POST", r"/v1/home/kids/chores/[^/]+/done"),
    ("POST", r"/v1/prompt/stream"),
    ("GET", r"/v1/turns/[^/]+"),
    ("DELETE", r"/v1/devices/[^/]+"),   # its own id only (may_revoke): unpair
))


def watch_route_allowed(method: str, path: str) -> bool:
    return any(m == method.upper() and rx.fullmatch(path) for m, rx in WATCH_ROUTES)


# A paired executor on another machine (titan-node#1631 slice 4; kind "executor", via "executor"): its presence and its own
# inbox, nothing else. It can't read its owner's items, memory, chats or anything else a Mac's token can; checked before
# ROUTE_ROLES. Its inbox key is always its own device id (agent_api_server), never one it names.
EXECUTOR_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((m, re.compile(p)) for m, p in (
    ("POST", r"/v1/device/presence"),
    ("GET", r"/v1/device/inbox"),
    ("POST", r"/v1/device/inbox/[^/]+/result"),
))


def executor_route_allowed(method: str, path: str) -> bool:
    return any(m == method.upper() and rx.fullmatch(path) for m, rx in EXECUTOR_ROUTES)


# API routes a non-admin role may call (method, path regex, roles). Everything else is admin-only.
# Routes that act on a person's own things (memory, their turns, their device) scope themselves.
_ALL = frozenset(ROLES)
_PEOPLE = frozenset({"admin", "adult", "kid"})
_SECURITY = frozenset({"admin", "adult", "household"})   # the house's locks, doors and cameras (#413): never kids
_AGENTS = frozenset({"admin", "adult"})   # whoever may use run_agent (the tool table): its runs' routes too (#699)
_ADULTS = frozenset({"admin", "adult"})   # the threads view (#951 (g)): kids and shared screens get none
_READERS = _ALL | {ENGINEERING}           # read routes an engineering agent may use; what it sees is decided per item
ROUTE_ROLES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = tuple(
    (m, re.compile(p), r) for m, p, r in (
        ("GET", r"/v1/me", _ALL),
        ("POST", r"/v1/speech", _ALL),                               # titan's voice for a reply (jellybean#709)
        ("POST", r"/v1/watch/pair/confirm", frozenset({"admin", "adult"})),   # a parent pairs a kid's Watch (#755)
        ("GET", r"/v1/watch/devices", frozenset({"admin", "adult"})),
        ("GET", r"/v1/watch/today", frozenset({"kid"})),
        ("POST", r"/v1/agent-runs", _AGENTS),                        # coding agents on the Mac, from the phone (#699)
        ("GET", r"/v1/agent-runs", _AGENTS),
        ("POST", r"/v1/agent-runs/[0-9a-f-]{36}/stop", _AGENTS),
        ("PATCH", r"/v1/me", _PEOPLE),
        ("PUT", r"/v1/people/[^/]+/call_me", frozenset({"adult"})),  # #1080: a parent for their kid (checked in code)                              # a person's own settings (hidden calendars)
        ("GET", r"/v1/conversations", _PEOPLE),                     # a person's own past conversations (#117)
        ("GET", r"/v1/conversations/search", _PEOPLE),              # searched, the person's own only (jellybean#827)
        ("GET", r"/v1/conversations/[^/]+/messages", _PEOPLE),
        ("POST", r"/v1/conversations/[^/]+/read", _PEOPLE),          # read marks, the person's own (#138)
        ("PATCH", r"/v1/conversations/[^/]+", _PEOPLE),             # file it under a project or group (#240)
        ("GET", r"/v1/projects", _PEOPLE),                          # projects and groups (jellybean#240): each
        ("POST", r"/v1/projects", _PEOPLE),                         # person's own, private by default; groups
        ("PATCH", r"/v1/projects/[^/]+", _PEOPLE),                  # are seen by their members only
        ("GET", r"/v1/projects/proposals", _PEOPLE),                # #1085: kids get none (in code)
        ("POST", r"/v1/projects/proposals/[^/]+/(keep|dismiss)", _PEOPLE),
        ("POST", r"/v1/projects/[^/]+/(split|merge)", _PEOPLE),
        ("POST", r"/v1/projects/[^/]+/related/[^/]+", _PEOPLE),   # #1109: kids are offered none (in code)
        ("POST", r"/v1/projects/undo", _PEOPLE),
        ("POST", r"/v1/items/[^/]+/(file|unfile)", _PEOPLE),
        ("POST", r"/v1/items/[^/]+/later", _PEOPLE),             # #1233: the caller's own Now; never a screen
        ("GET", r"/v1/groups", _PEOPLE),
        ("POST", r"/v1/groups", _PEOPLE),
        ("PATCH", r"/v1/groups/[^/]+", _PEOPLE),
        ("GET", r"/v1/groups/[^/]+/home", _PEOPLE),
        ("GET", r"/v1/sync", _PEOPLE),                              # a person's own continuity feed (#132/#138)
        ("GET", r"/v1/presence/context", _PEOPLE),                  # what their own devices were on (#132)
        ("POST", r"/v1/presence/context", _PEOPLE),
        ("DELETE", r"/v1/presence/context/[^/]+", _PEOPLE),      # picked up or dismissed elsewhere (#132)
        ("GET", r"/v1/timeline", _ALL),                             # what they may see (item 9)
        ("POST", r"/v1/timeline", _ALL),
        ("POST", r"/v1/prompt(/stream)?", _ALL),
        ("POST", r"/v1/device/(result|reply)/[^/]+", _ALL),
        ("POST", r"/v1/device/confirm/[^/]+", _ALL),                # answering a Mac's confirmation (own turns)
        ("GET", r"/v1/device/inbox", _PEOPLE),                      # a person's own Mac (cross_device.py)
        ("POST", r"/v1/device/inbox/[^/]+/result", _PEOPLE),
        ("GET", r"/v1/device/presence", _PEOPLE),
        ("POST", r"/v1/presence/place", _PEOPLE),
        ("GET", r"/v1/presence/regions", _PEOPLE),                   # own pending town/address triggers (#616)
        ("GET", r"/v1/presence/places", _PEOPLE),                    # own + a parent's kids' last place (#186)
        ("GET", r"/v1/home", _ALL),                                  # a shared screen's Home (#669): household items only
        ("GET", r"/v1/home/week", _ALL),                             # the shared screen's week (jellybean#417)
        ("GET", r"/v1/home/security", _SECURITY),                    # locks and doors, read-only (#413); never kids
        ("GET", r"/v1/home/cameras", _SECURITY),                     # camera stills (#413): HA_CAMERA_SNAPSHOTS=1
        ("GET", r"/v1/home/cameras/camera\.[A-Za-z0-9_]+/snapshot", _SECURITY),
        ("POST", r"/v1/home/cameras/camera\.[A-Za-z0-9_]+/live", _SECURITY),   # live video (#413): HA_CAMERA_LIVE=1
        ("GET", r"/v1/home/live/[0-9a-f]{32}/[A-Za-z0-9_./-]+", _SECURITY),   # (also reachable by its session id alone)
        ("DELETE", r"/v1/home/live/[0-9a-f]{32}", _SECURITY),
        ("POST", r"/v1/home/lock-up", _SECURITY),                    # Lock up (#413): lock only, HA_LOCK_UP=1
        ("GET", r"/v1/home/lock-up/[0-9a-f]{16}", _SECURITY),
        ("GET", r"/v1/references", _ALL),                            # pointers to device items, never bodies (#446)
        ("POST", r"/v1/references", _PEOPLE),                         # a person's own device writes its references
        ("POST", r"/v1/hooks", _PEOPLE),                              # a person's own device sends an item's link hooks (#951)
        ("GET", r"/v1/threads", _ADULTS),                             # the asker's threads as data (#951 (g))
        ("POST", r"/v1/threads/relate", _ADULTS),                     # the person's correction: related or not
        ("GET", r"/v1/surfaces/account", _ADULTS),                    # an account's page (#951 step 5)
        ("POST", r"/v1/obligations/acct-[0-9a-f]{16}/(say|share|take-back)", _ADULTS),   # their own word on it
        ("POST", r"/v1/contacts/sync", _ADULTS),                      # an adult's own Mac's contacts (#1380)
        ("DELETE", r"/v1/contacts/[^/]+", _ADULTS),
        ("GET", r"/v1/obligations/watch", _ADULTS),
        ("POST", r"/v1/places/confirm", _PEOPLE),                    # their own places (#1427): never a shared screen                   # what their device may match a text to (#951 step 4)
        ("POST", r"/v1/places", _ADULTS),                            # add a place (#1510); kids propose, can't change
        ("PATCH", r"/v1/places/[A-Za-z0-9-]+", _ADULTS),             # edit one of their own (or their kid's)
        ("DELETE", r"/v1/places/[A-Za-z0-9-]+", _ADULTS),            # archive it (undo)
        ("PATCH", r"/v1/references/rf-[0-9a-f]{16}", _PEOPLE),
        ("GET", r"/v1/home/kids", _ALL),                             # the kids' screen (jellybean#422)
        ("POST", r"/v1/home/kids/chores/[^/]+/done", frozenset({"household", "kid"})),   # the TV's one write: a checkmark
        ("POST", r"/v1/home/media", _ALL),                           # play/pause/skip what Home shows playing (jellybean#421)
        ("GET", r"/v1/home/house", _ALL),                            # the house room by room: lights, thermostats, scenes (jellybean#420)
        ("POST", r"/v1/home/house", _ALL),                           # what it offers, through home_control (kids: their domains)
        ("GET", r"/v1/family-meeting", _ALL),                        # the household's week (jellybean#425): household items only
        ("GET", r"/v1/our-year", _ALL),                            # the household's year, for the TV (jellybean#430)
        ("POST", r"/v1/our-year/photos", _PEOPLE),                 # a person's own shared-album photo counts
        ("POST", r"/v1/presence/room", _AGENTS),                   # #433: an adult's own room hint (kids: not yet)
        ("DELETE", r"/v1/presence/room", _AGENTS),
        ("POST", r"/v1/devices/self/activities", _PEOPLE),         # a Live Activity's push token (#725)
        ("POST", r"/v1/devices/self/trips", _PEOPLE),              # an upcoming trip's plan, for push-to-start (jellybean#753)
        ("DELETE", r"/v1/devices/self/trips/[^/]+", _PEOPLE),
        ("GET", r"/v1/me/prefs", _PEOPLE),                         # a person's look across devices (#1159)
        ("POST", r"/v1/me/executor-pairing", _ADULTS),             # pair one of their own other machines (#1631)
        ("GET", r"/v1/me/triage-rules", _PEOPLE),                  # what they taught mail triage (#1644)
        ("DELETE", r"/v1/me/triage-rules/[^/]+", _PEOPLE),
        ("GET", r"/v1/me/triage-score", _PEOPLE),                  # how triage matches what they do (#1644)
        ("PUT", r"/v1/me/prefs", _PEOPLE),
        ("GET", r"/v1/me/clearing", _PEOPLE),                      # what "clear this" means for them (#1646)
        ("PUT", r"/v1/me/clearing", _PEOPLE),
        ("GET", r"/v1/me/capability-log", _PEOPLE),                # what titan did for them (#1628)
        ("GET", r"/v1/me/capabilities", _PEOPLE),                  # their own capability tiers (#1628)
        ("PUT", r"/v1/me/capabilities", _PEOPLE),
        ("GET", r"/v1/me/autonomy", _PEOPLE),                      # what titan may do without asking (#1645)
        ("PUT", r"/v1/me/autonomy", _PEOPLE),
        ("GET", r"/v1/sharing/grants", _PEOPLE),                   # a person's own "Always for <name>" (#446)
        ("DELETE", r"/v1/sharing/grants/[^/]+", _PEOPLE),          # …and taking one back
        ("POST", r"/v1/sharing/grants/stop", _PEOPLE),             # …or all for one person, one undo (#1060)
        ("GET", r"/v1/surfaces/(household-facts|always-shared)", _PEOPLE),   # Settings lists as surfaces (#1060)
        ("POST", r"/v1/surfaces/household-facts/confirm", _ADULTS),           # confirm a whole entity's stale facts
        ("GET", r"/v1/surfaces/review", _PEOPLE),                   # bulk review of scanned mail (#1507; a kid sees, can't file)
        ("POST", r"/v1/surfaces/review/(approve|exclude)", _ADULTS),  # approve a group / leave one out; each with an undo token
        ("POST", r"/v1/surfaces/undo", _PEOPLE),                    # a batch's one undo, the caller's own token
        ("GET", r"/v1/surfaces/rooms", _PEOPLE),                    # Settings › Rooms (#1511; kids see, can't change)
        ("POST", r"/v1/rooms", _ADULTS),                            # add a room
        ("PATCH", r"/v1/rooms/[A-Za-z0-9-]+", _ADULTS),             # rename (screens follow; HA area unchanged)
        ("DELETE", r"/v1/rooms/[A-Za-z0-9-]+", _ADULTS),            # remove (screens lose it; undo)
        ("POST", r"/v1/devices/push", _ALL),                         # a device's own APNs token (push.py)                    # a person's own arrivals (places.py)
        ("POST", r"/v1/device/presence", _PEOPLE),
        ("GET", r"/v1/turns/[^/]+", _ALL),
        ("POST", r"/v1/turns/[^/]+/cancel", _ALL),                      # Stop, by name (jellybean#369); owner-checked
        ("GET", r"/v1/files/generated/[^/]+", _ALL),                # a picture the caller made (#201 slice 2)
        ("POST", r"/v1/files", _PEOPLE),                            # a person's file for a turn (titan-node#713)
        ("POST", r"/v1/voice-notes", _PEOPLE),                      # a voice note to the household (jellybean#313)
        ("GET", r"/v1/handoff/rooms", _PEOPLE),                     # continue on another room's screen (jellybean#433)
        ("POST", r"/v1/handoff", _PEOPLE),
        ("GET", r"/v1/handoff", _PEOPLE),
        ("GET", r"/v1/handoff/self", frozenset({"household"})),
        ("DELETE", r"/v1/handoff/[0-9a-f]{16}", _ALL),                # the sender takes it back, or the screen's Done
        ("GET", r"/v1/rooms/state", frozenset({"admin", "adult", "household"})),   # a room's state, never a person's (#433)
        ("POST", r"/v1/tv-unlocks/request", frozenset({"household"})),   # a TV asks an adult to unlock it (jellybean#427)
        ("GET", r"/v1/tv-unlocks/self", frozenset({"household"})),
        ("GET", r"/v1/tv-unlocks/self/layer", frozenset({"household"})),
        ("DELETE", r"/v1/tv-unlocks/self", frozenset({"household"})),
        ("DELETE", r"/v1/tv-unlocks/[0-9a-f]{16}", frozenset({"admin", "adult"})),   # Lock now: their own only
        ("GET", r"/v1/home/sitter", frozenset({"household", "adult"})),  # sitter mode (jellybean#428, sitter.py): the TV's view
        ("POST", r"/v1/home/sitter", frozenset({"household", "adult"})),  # an adult starts it; a TV asks the adults
        ("DELETE", r"/v1/home/sitter", frozenset({"adult"})),             # an adult ends it; never the TV alone
        ("GET", r"/v1/home/sitter/sheet", frozenset({"adult"})),          # the adults' own edit view
        ("PUT", r"/v1/home/sitter/sheet", frozenset({"adult"})),
        ("GET", r"/v1/household/people", _PEOPLE),                 # who's in the household: id, name, role (#1164)
        ("GET", r"/v1/household/voice-notes", _ALL),                # whether kids may send them
        ("PATCH", r"/v1/household/voice-notes", _PEOPLE),          # an adult's household setting
        ("GET", r"/v1/files/uploads/[0-9a-f]{24}", _ALL),           # its owner and audience (uploads.for_viewer)
        ("GET", r"/v1/fences", _PEOPLE),                             # their own fences and counts (#357)
        ("DELETE", r"/v1/fences/\d+", _PEOPLE),                     # retire their own
        ("DELETE", r"/v1/devices/[^/]+", _ALL),                    # admin, or the device itself
        ("PATCH", r"/v1/devices/self", _ALL),                       # a device names its own room (jellybean#424)
        ("POST", r"/v1/memory/recall", _PEOPLE),
        ("GET", r"/v1/memory/facts", _PEOPLE),
        ("POST", r"/v1/memory/facts", _PEOPLE),
        ("PATCH", r"/v1/memory/facts/[^/]+", _PEOPLE),
        ("DELETE", r"/v1/memory/facts/[^/]+", _PEOPLE),
        ("POST", r"/v1/memory/facts/[^/]+/restore", _PEOPLE),
        ("GET", r"/v1/memory/groups", _PEOPLE),                  # #1073
        ("GET", r"/v1/memory/review", _PEOPLE),                  # #1074: the caller's own queue
        ("POST", r"/v1/memory/review/[^/]+", _PEOPLE),
        ("POST", r"/v1/memory/forget", _PEOPLE),
        ("POST", r"/v1/memory/restore", _PEOPLE),
        ("GET", r"/v1/memory/dev_notes", _PEOPLE),               # #1605: the caller's own wishes about titan
        ("GET", r"/v1/kb(/[^/]+)?", _READERS),                    # what the caller may see (kb_privacy)
        ("GET", r"/v1/world/entities(/[^/]+)?", _READERS),        # world pages, per viewer (world.py, #365)
        ("GET", r"/v1/host/summary", frozenset({ENGINEERING})),     # titan's own health (disks, GPUs, units): the titan MCP
        ("GET", r"/v1/schedule", _ALL),
        ("GET", r"/v1/box/status", _ALL),                        # #407: the light's colour (the admin also gets its sentence)
        ("GET", r"/v1/brief", _ALL),                             # #1060: a composition of the caller's own schedule, items, cards
        ("GET", r"/v1/public/now", _ALL),
        ("GET", r"/v1/me/sharing", frozenset({"adult", "kid"})),
        ("GET", r"/v1/me/assistant", frozenset({"adult", "kid"})),   # #1076, the caller's own
        ("GET", r"/v1/about", _ALL),
        ("POST", r"/v1/reports", frozenset({"adult", "kid"})),
        ("GET", r"/v1/reports", frozenset({"adult", "kid"})),        # their own; the admin's queue is everyone's
        ("GET", r"/v1/me/data", frozenset({"adult", "kid"})),
        ("GET", r"/v1/me/references", frozenset({"adult", "kid"})),   # #1135: the caller's own
        ("GET", r"/v1/me/places", frozenset({"adult", "kid"})),       # #1133: the caller's own places and phone mode
        ("POST", r"/v1/me/places/mode", frozenset({"adult", "kid"})),        # #1079, the caller's own
        ("GET", r"/v1/items", _ALL),                                # #1084: per viewer; a screen sees the household's
        ("POST", r"/v1/me/export", frozenset({"adult"})),            # kids: a parent exports for them (later)
        ("GET", r"/v1/me/export", frozenset({"adult"})),
        # #1080: kids reach it so me_delete can say "ask a parent"; it refuses them every action
        ("GET", r"/v1/me/delete", frozenset({"adult", "kid"})),
        ("POST", r"/v1/me/delete", frozenset({"adult", "kid"})),
        ("POST", r"/v1/me/delete/cancel", frozenset({"adult", "kid"})),
        ("GET", r"/v1/documents", _ALL),                         # household documents only (#320), every role
        ("GET", r"/v1/documents/[^/]+/content", _ALL),
        ("PUT", r"/v1/references/[^/]+/file_share", frozenset({"adult", "kid"})),   # #1081: kids refused in code
        ("GET", r"/v1/references/file_reads", frozenset({"adult", "kid"})),                        # public-read: the /now feed (#1043), nothing private                          # household schedules, per viewer (jellybean#219)
        ("GET", r"/v1/connectors/tombstones", _ALL),             # their own forgotten items and sources (#233)
        ("GET", r"/v1/connectors/state", _ALL),                  # the ledger epoch + their own ingest error counts
        ("DELETE", r"/v1/connectors/[a-z]+", frozenset({"adult", "kid"})),   # forget a source: their own items only
        ("GET", r"/v1/retrieval/trace", _ALL),                   # a person's own search trace; the admin's any (#368)
        ("POST", r"/v1/world/entities/[^/]+/facts", frozenset({"admin", "adult"})),   # set_fact (#358)
        ("POST", r"/v1/kb", frozenset({"admin", "adult"})),
        ("PUT", r"/v1/kb/[^/]+", frozenset({"admin", "adult"})),     # the owner, or the admin
        ("DELETE", r"/v1/kb/[^/]+", frozenset({"admin", "adult"})),
        ("POST", r"/v1/kb/[^/]+/(share|unshare)", frozenset({"admin", "adult"})),
        ("GET", r"/v1/proactive", _ALL),                           # only the caller's own cards (proactive.py)
        ("POST", r"/v1/proactive/[^/]+/(ack|action|snooze|mute)", _ALL),   # snooze/mute: the caller's own (#1039)
        ("GET", r"/v1/proactive/mutes", _ALL),
        ("GET", r"/v1/proactive/(sources|delivery)", _ALL),      # the caller's own (#1039, 10-06)
        ("PUT", r"/v1/proactive/(sources|delivery)", _ALL),
        ("DELETE", r"/v1/proactive/mutes", _ALL),
        ("GET", r"/v1/cards/wait", _ALL),
        ("POST", r"/v1/cards", _ALL),                               # who may send to whom: card_send_refusal
        ("GET", r"/v1/claude-chats", frozenset({"admin", "adult"})),     # the caller's own (claude_chats.py)
        ("DELETE", r"/v1/claude-chats", frozenset({"admin", "adult"})),
        ("POST", r"/v1/claude-chats/(sessions|import)", frozenset({"admin", "adult"})),
        ("GET", r"/v1/todos", _ALL),                               # the lists the caller may see (todos.py)
        ("POST", r"/v1/todos", _ALL),
        ("POST", r"/v1/todos/[^/]+/done", _ALL),
        ("GET", r"/v1/routines(/[^/]+)?", _ALL),
        ("POST", r"/v1/routines", frozenset({"admin", "adult", "household"})),
        ("DELETE", r"/v1/routines/[^/]+", frozenset({"admin", "adult", "household"})),
        ("GET", r"/v1/scheduled", _PEOPLE),                         # the caller's own (scheduled.py)
        ("DELETE", r"/v1/scheduled/[^/]+", _PEOPLE),                # its owner, or the admin
        ("GET", r"/v1/grants", _PEOPLE),                            # the caller's own grants (grants.py, #395)
        ("POST", r"/v1/grants", _PEOPLE),                           # a device registers what its person approved
        ("DELETE", r"/v1/grants/[^/]+", _PEOPLE),                   # its owner, or the admin
    ))


def card_audience_ids(aud: str, store: Any) -> tuple[str, list[str]]:
    """A person-list audience with names turned into ids: (audience, the entries matching nobody)."""
    if not aud.startswith("["):
        return aud, []
    people = store.people() if store else []
    out, unknown = [], []
    for x in json.loads(aud):
        hit = next((p["id"] for p in people if x in (p["id"].lower(), p["name"].lower())), None)
        (out.append(hit) if hit else unknown.append(x))
    return json.dumps(sorted(set(out))), unknown


def card_send_refusal(role: str | None, person_id: str, aud: str, store: Any = None) -> str | None:
    """Why this sender may not send a card to that audience (None: allowed). Adults and the admin
    send to anyone; a kid only to themself or the grown-ups; a household device to everyone."""
    if role in (None, "admin", "adult", "household"):
        return None
    if aud in ("adults", "admins"):
        return None
    if aud == "household":
        return "A card to the whole household comes from a grown-up."
    try:
        ids = set(json.loads(aud))
    except ValueError:
        return "Unknown audience."
    grown = {p["id"] for p in (store.people() if store else []) if p.get("role") in ("admin", "adult")}
    return None if ids <= grown | {person_id} else "You can send cards to yourself or the grown-ups."


def route_allowed(role: str | None, method: str, path: str) -> bool:
    if not role or role == "admin":
        return True
    return any(m == method.upper() and rx.fullmatch(path) and role in roles for m, rx, roles in ROUTE_ROLES)


def capabilities(role: str) -> dict[str, bool]:
    """/v1/me's `can`: a flat map the clients use to hide what the person can't use (titan still
    enforces every one of these itself)."""
    t = lambda *tools: all(role_allows_tool(role, x) for x in tools)   # noqa: E731
    r = lambda method, path: route_allowed(role, method, path)         # noqa: E731
    return {
        "chat": True,
        "house_control": t("home_control"),
        "house_security": role not in SECURITY_HIDDEN_ROLES,           # see locks/cameras/alarms
        "calendar": t("list_calendar_events", "create_calendar_event"),
        "reminders": t("list_reminders", "create_reminder"),
        "weather": t("get_weather"),
        "web": t("web_search"),
        "knowledge_base": t("kb_retrieve"),
        "knowledge_base_edit": t("ingest_text", "kb_forget") and r("POST", "/v1/kb"),
        "knowledge_base_share": t("share", "unshare") and r("POST", "/v1/kb/x/share"),
        "routines": t("list_routines"),
        "routines_edit": t("save_routine") and r("POST", "/v1/routines"),
        "scheduled": t("schedule_action") and r("GET", "/v1/scheduled"),
        "personal_memory": r("GET", "/v1/memory/facts"),
        "contacts": t("search_contacts"),
        "messages": t("send_message"),
        "messages_read": t("search_messages"),
        "location": t("find_my_locate"),
        "mail": t("draft_email"),
        "notes": t("search_notes"),
        "files": t("read_file", "write_file", "move_files", "trash_files"),
        "shell": t("run_shell", "run_applescript"),
        "agents": t("run_agent"),
        "screen": t("get_screen", "ax_click"),
        "skills": t("run_skill", "create_skill"),
        "records": t("find_records"),
        "timeline": t("recall_events"),
        "manage_household": role == "admin",
        "host_ops": t("host_status", "run_check"),
        "workspaces": r("POST", "/v1/workspaces"),
        "cards": r("GET", "/v1/proactive"),
    }


def conv_id_for(person_id: str) -> str:
    """The memory conversation a person's chat turns go to. Zach keeps "chat", so the history from
    before people existed stays his."""
    return "chat" if person_id == ADMIN_PERSON_ID else f"chat:{person_id}"


# ════════════════════════════════════════════════════════════════════════════════════════════
# Storage
# ════════════════════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Identity:
    person: dict[str, str]                 # {id, name, role}
    device: dict[str, str] | None          # {id, name, platform}; None for the master token
    role: str
    via: str                               # master | device | ase | engineering | open (no auth configured)

    @property
    def person_id(self) -> str:
        return self.person["id"]

    @property
    def first_name(self) -> str:
        return (self.person.get("name") or "").split(" ")[0] or "someone"

    @property
    def call_me(self) -> str:
        """What titan calls them in replies, greetings and on the TV (#1080): their own choice, else the first name.
        Never used to decide who they are."""
        if self.role in ("household", ENGINEERING):
            return self.first_name
        try:
            return get_store().call_me(self.person_id) or self.first_name
        except Exception:  # noqa: BLE001 - a store hiccup never blocks a turn
            return self.first_name


def engineering_identity() -> Identity:
    return Identity({"id": ENGINEERING, "name": "Engineering agent", "role": ENGINEERING}, None, ENGINEERING,
                    ENGINEERING)


def _engineering_token() -> str:
    path = os.environ.get("AGENT_ENGINEERING_TOKEN_FILE", "")
    if not path:
        return ""
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


_CALL_ME_RX = re.compile(r"^[^\W\d_][\w .'\u2019-]{0,29}$")


def clean_call_me(value: Any) -> str | None:
    """1–30 characters, starting with a letter; letters, digits, spaces, apostrophes, periods and hyphens. → the
    cleaned value, None to clear, or PairingError 400."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise PairingError(400, "call_me is text, 1-30 characters")
    import untrusted
    v = untrusted.clean_name(value, limit=60)
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or not _CALL_ME_RX.match(v) or v != " ".join(value.split()):
        raise PairingError(400, "call_me: 1-30 characters, starting with a letter (letters, digits, spaces, ' . -)")
    return v


def owner_identity(via: str = "master", name: str = ADMIN_PERSON_NAME) -> Identity:
    return Identity({"id": ADMIN_PERSON_ID, "name": name, "role": "admin"}, None, "admin", via)


# A person's facets (#354, docs/world-model-bubbles.md); home is the household's, inbox is unmapped.
FACETS = ("personal", "social", "work", "school")


class PairingError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _sources_of(r: Any) -> dict[str, dict[str, Any]] | None:
    """A Mac's sync sources ({mail: {level, since}}), None before jellybean#413."""
    try:
        raw = r["sources"] if "sources" in r.keys() else None
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()


def new_device_token() -> str:
    """32 random bytes, base64url without padding (43 characters)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


_TOKEN_RX = re.compile(r"(?:x\.)?[A-Za-z0-9_-]{43}")
# An executor's token (#1631) is "x." + a device token: a code from before executors (a rollback) rejects its shape
# outright, so it can never be read as an ordinary device with its owner's role. Every other token has no prefix.
EXECUTOR_TOKEN_PREFIX = "x."
# The device kinds this code knows; a row of any other kind (one a later version made) resolves to nothing.
DEVICE_KINDS = frozenset({None, "device", "watch", "tv", "executor"})


class ClaimLimiter:
    """Claim rate limits: CLAIMS_PER_IP_PER_MIN attempts per client IP per minute, and after
    GLOBAL_FAILURES_PER_HOUR failed claims in an hour every claim is refused for LOCKOUT_S.
    In memory: a restart clears it (and ends any lockout), which is fine for a home server."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._per_ip: dict[str, deque[float]] = {}
        self._failures: deque[float] = deque()
        self._locked_until = 0.0

    def admit(self, ip: str) -> bool:
        now = self._now()
        with self._lock:
            if now < self._locked_until:
                return False
            q = self._per_ip.setdefault(ip or "?", deque())
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= CLAIMS_PER_IP_PER_MIN:
                return False
            q.append(now)
            if len(self._per_ip) > 1000:       # forget idle IPs
                for k in [k for k, v in self._per_ip.items() if not v or v[-1] <= now - 60]:
                    self._per_ip.pop(k, None)
            return True

    def failed(self) -> None:
        now = self._now()
        with self._lock:
            self._failures.append(now)
            while self._failures and self._failures[0] <= now - 3600:
                self._failures.popleft()
            if len(self._failures) >= GLOBAL_FAILURES_PER_HOUR:
                self._locked_until = now + LOCKOUT_S
                self._failures.clear()


# A kid's Watch pairing, per source (10-07 review W6): one device on the network can neither hold the household's
# open codes (MAX_OPEN_CODES is for everyone) nor lock every Watch out by failing on purpose. Codes a source may have
# open at once (they live CODE_TTL_S), and failures that close pairing to that source, not to everyone, for LOCKOUT_S.
WATCH_CODES_PER_SOURCE = 2
WATCH_FAILURES_PER_SOURCE = 10


class WatchPairLimiter(ClaimLimiter):
    """ClaimLimiter's per-source rate, plus per-source caps: codes issued (issue) and failures (failed(source)), each
    locking out only that source. Sources are a client address, or "dev:<id>" for a parent's device confirming."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__(now)
        self._codes: dict[str, deque[float]] = {}
        self._fails: dict[str, deque[float]] = {}
        self._blocked: dict[str, float] = {}

    def _recent(self, book: dict[str, deque[float]], key: str, span: float, now: float) -> deque[float]:
        q = book.setdefault(key or "?", deque())
        while q and q[0] <= now - span:
            q.popleft()
        if len(book) > 1000:   # forget idle sources
            for k in [k for k, v in book.items() if not v]:
                book.pop(k, None)
        return q

    def blocked(self, source: str) -> bool:
        with self._lock:
            return self._now() < self._blocked.get(source or "?", 0.0)

    def admit(self, ip: str) -> bool:
        return not self.blocked(ip) and super().admit(ip)

    def issue(self, source: str) -> bool:
        """A new code for this source, unless it already has WATCH_CODES_PER_SOURCE open."""
        now = self._now()
        with self._lock:
            q = self._recent(self._codes, source, CODE_TTL_S, now)
            if len(q) >= WATCH_CODES_PER_SOURCE:
                return False
            q.append(now)
            return True

    def failed(self, source: str = "?") -> None:   # type: ignore[override]
        now = self._now()
        with self._lock:
            q = self._recent(self._fails, source, 3600, now)
            q.append(now)
            if len(q) >= WATCH_FAILURES_PER_SOURCE:
                self._blocked[source or "?"] = now + LOCKOUT_S
                q.clear()


# person_for_calendar's answer for a title two or more people claim (#1234): an owner no viewer is, with no name to
# attribute events to.
AMBIGUOUS_CALENDAR: dict[str, str] = {"id": "?ambiguous", "name": "", "role": "adult"}


class HouseholdStore:
    def __init__(self, path: str | Path | None = None, *, now: Callable[[], float] = time.time,
                 limiter: ClaimLimiter | None = None) -> None:
        self.path = Path(path or os.environ.get("HOUSEHOLD_DB", "/ai/datasets/connectors/household.db"))
        self._now = now
        self.limiter = limiter or ClaimLimiter()
        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}
        self._versions: dict[str, str] = {}
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if new:
            try:
                os.chmod(self.path, 0o600)     # token hashes: the service user only
            except OSError:
                pass
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), name TEXT NOT NULL,
                platform TEXT NOT NULL, token_sha256 TEXT NOT NULL UNIQUE, created_at REAL NOT NULL,
                last_seen REAL, revoked_at REAL);
            CREATE INDEX IF NOT EXISTS idx_devices_person ON devices(person_id);
            CREATE TABLE IF NOT EXISTS pair_codes (
                code TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id), label TEXT,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, device_id TEXT);
            CREATE TABLE IF NOT EXISTS device_calendars (
                device_id TEXT NOT NULL, cal_id TEXT NOT NULL, person_id TEXT NOT NULL, title TEXT, at REAL NOT NULL,
                PRIMARY KEY (device_id, cal_id));
            CREATE TABLE IF NOT EXISTS mac_presence (
                person_id TEXT NOT NULL, device_key TEXT NOT NULL, device_id TEXT, name TEXT NOT NULL,
                lan_ip TEXT, mac_address TEXT, wake_port INTEGER, on_tailnet INTEGER, awake INTEGER,
                updated_at REAL NOT NULL, PRIMARY KEY (person_id, device_key));
            """)
            self._db.execute("INSERT OR IGNORE INTO people (id, name, role, created_at) VALUES (?,?,?,?)",
                             (ADMIN_PERSON_ID, ADMIN_PERSON_NAME, "admin", self._now()))
            mcols = [r[1] for r in self._db.execute("PRAGMA table_info(mac_presence)")]
            for col, decl in (("battery_pct", "INTEGER"), ("on_ac", "INTEGER"), ("disk_free_gb", "REAL"),
                              ("app_version", "TEXT"), ("device_tools", "TEXT"), ("sources", "TEXT"),
                              ("kind", "TEXT")):   # #1631: "executor" for a paired executor; every older row is a Mac
                if col not in mcols:   # device health v2: the Mac's battery and free disk; then its app version
                    self._db.execute(f"ALTER TABLE mac_presence ADD COLUMN {col} {decl}")
            if "app_version" not in [r[1] for r in self._db.execute("PRAGMA table_info(devices)")]:
                self._db.execute("ALTER TABLE devices ADD COLUMN app_version TEXT")   # X-Client-Version
            if "holds" not in [r[1] for r in self._db.execute("PRAGMA table_info(devices)")]:
                self._db.execute("ALTER TABLE devices ADD COLUMN holds TEXT")   # #776: kinds an iPhone answers item_body for
            if "kind" not in [r[1] for r in self._db.execute("PRAGMA table_info(pair_codes)")]:
                self._db.execute("ALTER TABLE pair_codes ADD COLUMN kind TEXT")   # #1631: "executor", set by its owner
            if "room" not in [r[1] for r in self._db.execute("PRAGMA table_info(devices)")]:
                self._db.execute("ALTER TABLE devices ADD COLUMN room TEXT")   # the room it's in (jellybean#424)
            if "kind" not in [r[1] for r in self._db.execute("PRAGMA table_info(devices)")]:
                # "watch": a kid's Watch paired directly (titan-node#755); every older row is an ordinary device.
                self._db.execute("ALTER TABLE devices ADD COLUMN kind TEXT NOT NULL DEFAULT 'device'")
            # A kid's Watch asking to be paired (titan-node#755): the code it shows, the poll secret's sha256 (the
            # secret itself never stored or logged), and the parent's confirmation. The token is made at the poll.
            self._db.execute("""CREATE TABLE IF NOT EXISTS watch_pairs (
                request_id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, secret_sha256 TEXT NOT NULL,
                device_name TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, person_id TEXT,
                confirmed_by TEXT, attempts INTEGER NOT NULL DEFAULT 0, device_id TEXT, closed_at REAL)""")
            # A shared screen asks the same way (#411, #434: an Apple TV pairs with no typing on the remote): kind
            # "screen", the name and room the admin gave, and the device they confirmed on (shown on the TV).
            pcols = [r[1] for r in self._db.execute("PRAGMA table_info(watch_pairs)")]
            for col, decl in (("kind", "TEXT NOT NULL DEFAULT 'watch'"), ("screen_name", "TEXT"), ("room", "TEXT"),
                              ("confirmed_device", "TEXT")):
                if col not in pcols:
                    self._db.execute(f"ALTER TABLE watch_pairs ADD COLUMN {col} {decl}")
            cols = [r[1] for r in self._db.execute("PRAGMA table_info(people)")]
            for col, decl in (("birth_year", "INTEGER"), ("contact_id", "TEXT"), ("contact_name", "TEXT"),
                              ("calendars", "TEXT"), ("prefs", "TEXT"), ("calendars_ignored", "TEXT"),
                              ("places", "TEXT"), ("parents", "TEXT"), ("facets", "TEXT"), ("other_parent", "TEXT"),
                              ("aliases", "TEXT"), ("call_me", "TEXT"),
                              # a member's own email addresses, keyed only (#1424): hooks.keyed("addr", …) values, a JSON
                              # list, so an attendee or mail party given as an address resolves to them. Never raw.
                              ("addr_keys", "TEXT")):
                if col not in cols:   # household facts (Settings › Household): all optional
                    self._db.execute(f"ALTER TABLE people ADD COLUMN {col} {decl}")

    # ── a member's own addresses, keyed (#1424) ───────────────────────────────────────────────

    _ADDR_KEY_RX = re.compile(r"[0-9a-f]{16,128}")

    def set_addr_keys(self, person_id: str, keys: list[str]) -> int:
        """Replace this person's addr_keys. Keys only: a value that isn't a hex HMAC is refused (the message never
        repeats it), so a raw address can't be stored by mistake."""
        keys = list(dict.fromkeys(str(k) for k in keys))
        if any(not self._ADDR_KEY_RX.fullmatch(k) for k in keys):
            raise ValueError("member addresses are stored keyed only")
        with self._lock:
            cur = self._db.execute("UPDATE people SET addr_keys=? WHERE id=?", (json.dumps(keys) if keys else None, person_id))
            if not cur.rowcount:
                raise PairingError(404, "no such person")
        return len(keys)

    def addr_keys(self) -> dict[str, list[str]]:
        """{person id: [keyed address, …]}: what graph_sync resolves attendees and mail parties by."""
        with self._lock:
            rows = self._db.execute("SELECT id, addr_keys FROM people WHERE addr_keys IS NOT NULL").fetchall()
        return {r["id"]: json.loads(r["addr_keys"]) for r in rows}

    # ── people ───────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _person(row: sqlite3.Row) -> dict[str, str]:
        return {"id": row["id"], "name": row["name"], "role": row["role"]}

    def person(self, person_id: str) -> dict[str, str] | None:
        row = self._db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        return self._person(row) if row else None

    @staticmethod
    def _facts(row: sqlite3.Row) -> dict[str, Any]:
        """The household facts about a person (Settings › Household): birth year and the age it gives,
        their contact card, the calendars that are theirs. Absent facts are null / []."""
        try:
            cals = json.loads(row["calendars"] or "[]")
        except ValueError:
            cals = []
        by = row["birth_year"]
        try:
            ignored = json.loads(row["calendars_ignored"] or "[]") if "calendars_ignored" in row.keys() else []
        except ValueError:
            ignored = []
        try:
            named = json.loads(row["places"] or "[]") if "places" in row.keys() else []
        except ValueError:
            named = []
        try:
            parents = json.loads(row["parents"] or "[]") if "parents" in row.keys() else []
        except ValueError:
            parents = []
        try:
            facets = json.loads(row["facets"] or "{}") if "facets" in row.keys() else {}
        except ValueError:
            facets = {}
        try:   # what the household also calls them ("Jo" for Jordan; 10-02 review)
            aliases = json.loads(row["aliases"] or "[]") if "aliases" in row.keys() else []
        except ValueError:
            aliases = []
        return {"birth_year": by, "age": (time.localtime().tm_year - by) if by else None,
                "calendars_ignored": ignored, "places": named, "parents": parents,
                "facets": facets if isinstance(facets, dict) else {},
                "aliases": aliases if isinstance(aliases, list) else [],
                # a kid's parent outside the household, as the household says it ("their dad"; custody.py): never
                # one of the household's adults
                "other_parent": (row["other_parent"] if "other_parent" in row.keys() else None) or None,
                "contact": {"id": row["contact_id"], "name": row["contact_name"] or ""} if row["contact_id"] else None,
                "calendars": cals,
                # what titan calls them (#1080): their own words, never their identity (the roster name stays that)
                "call_me": (row["call_me"] if "call_me" in row.keys() else None) or None}

    def call_me(self, person_id: str) -> str | None:
        row = self._db.execute("SELECT call_me FROM people WHERE id=?", (person_id,)).fetchone()
        return (row["call_me"] if row else None) or None

    def set_call_me(self, person_id: str, value: Any) -> str | None:
        """What titan calls a person (#1080): 1–30 characters, one line, letters first; cleaned like any untrusted
        name (untrusted.clean_name: no markers, no control characters). None or "" clears it (back to the first
        name). Who may set it is the route's to check (may_set_call_me)."""
        v = clean_call_me(value)
        with self._lock:
            if not self._db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise PairingError(404, "no such person")
            self._db.execute("UPDATE people SET call_me=? WHERE id=?", (v, person_id))
            self._db.commit()
        return v

    def may_set_call_me(self, editor: "Identity", person_id: str) -> bool:
        """Each person sets their own; a kid's may also be set by their parent or the admin. The admin can't change
        another adult's, and nobody but the person sets an adult's."""
        if editor.role == "household" or not editor.person_id:
            return False
        if editor.person_id == person_id:
            return True
        target = self.person(person_id)
        if not target or target["role"] != "kid":
            return False
        return editor.role == "admin" or (editor.role == "adult" and editor.person_id in self.parents_of(person_id))

    def person_facts(self, person_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
        return {**self._person(row), **self._facts(row)} if row else None

    def prefs(self, person_id: str) -> dict[str, str]:
        """How this person wants titan to talk to them (PREFERENCES), with the defaults filled in."""
        row = self._db.execute("SELECT prefs FROM people WHERE id=?", (person_id,)).fetchone()
        try:
            saved = json.loads(row["prefs"] or "{}") if row else {}
        except ValueError:
            saved = {}
        return {k: saved.get(k, d) for k, (d, _) in PREFERENCES.items()}

    def set_pref(self, person_id: str, name: str, value: str) -> dict[str, str]:
        if name not in PREFERENCES:
            raise PairingError(400, f"unknown preference; one of: {', '.join(PREFERENCES)}")
        if value not in ("on", "off"):
            raise PairingError(400, "value must be on or off")
        with self._lock:
            row = self._db.execute("SELECT prefs FROM people WHERE id=?", (person_id,)).fetchone()
            if not row:
                raise PairingError(404, "no such person")
            try:
                saved = json.loads(row["prefs"] or "{}")
            except ValueError:
                saved = {}
            saved[name] = value
            self._db.execute("UPDATE people SET prefs=? WHERE id=?", (json.dumps(saved), person_id))
        return self.prefs(person_id)

    def set_calendars_ignored(self, person_id: str, titles: Any) -> list[str]:
        """The calendars this person hides (Jellybean's calendar picker, Mac or iPhone): their devices
        leave them out of the agenda and titan ignores them. The person's own setting, shared by all
        their devices."""
        if not isinstance(titles, list):
            raise PairingError(400, "calendars_ignored must be a list of calendar titles")
        clean: list[str] = []
        for t in titles:
            name = " ".join(str(t or "").split())[:80]
            if name and name.lower() not in (c.lower() for c in clean):
                clean.append(name)
        with self._lock:
            if not self._db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise PairingError(404, "no such person")
            self._db.execute("UPDATE people SET calendars_ignored=? WHERE id=?", (json.dumps(clean[:50]), person_id))
        return clean[:50]

    def set_places(self, person_id: str, names: Any) -> list[str]:
        """The places this person named on their iPhone (Settings › Places): names only, never where they
        are. titan learns them here instead of waiting for a first arrival (#284: "when I get home…")."""
        if not isinstance(names, list):
            raise PairingError(400, "places must be a list of place names")
        clean: list[str] = []
        for t in names:
            name = " ".join(str(t or "").split())[:40]
            if name and name.lower() not in (c.lower() for c in clean):
                clean.append(name)
        with self._lock:
            if not self._db.execute("SELECT 1 FROM people WHERE id=?", (person_id,)).fetchone():
                raise PairingError(404, "no such person")
            self._db.execute("UPDATE people SET places=? WHERE id=?", (json.dumps(clean[:20]), person_id))
        return clean[:20]

    def set_facets(self, person_id: str, mapping: Any, merge: bool = False) -> dict[str, str]:
        """This person's facet map (#354): which part of their life each of their calendars and bill vendors
        belongs to. Keys "calendar:<title>", "vendor:<name>" or "mailbox:<titan's account label>" (#1490) (lowercased),
        values FACETS; or "sender:<name>" with the value "junk", the owner's own sender-is-junk rule (#1506; a scan's
        sender, whole name; it hides nothing of anyone else's). A None value
        removes a key when merging. Replaced whole unless merge. The world sync files their items by it; it
        never changes who sees them."""
        if not isinstance(mapping, dict):
            raise PairingError(400, "facets must be an object of {\"calendar:<title>\" | \"vendor:<name>\" | "
                                    "\"mailbox:<label>\": facet}")
        with self._lock:
            row = self._db.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()
            if not row:
                raise PairingError(404, "no such person")
            out = dict(self._facts(row)["facets"]) if merge else {}
            for k, v in mapping.items():
                kind, _, name = str(k or "").partition(":")
                key = f"{kind.strip().lower()}:{' '.join(name.split()).lower()[:80]}"
                if kind.strip().lower() not in ("calendar", "vendor", "mailbox", "sender") or not name.strip():
                    raise PairingError(400, f"a facet key is calendar:<title>, vendor:<name>, mailbox:<label> or "
                                            f"sender:<name>, not {str(k)[:40]!r}")
                if v is None and merge:
                    out.pop(key, None)
                    continue
                if kind.strip().lower() == "sender":
                    if v != "junk":
                        raise PairingError(400, "a sender rule's value is junk")
                    out[key] = v
                    continue
                if v not in FACETS:
                    raise PairingError(400, f"a facet is one of {', '.join(FACETS)}")
                out[key] = v
            out = dict(list(out.items())[:200])
            self._db.execute("UPDATE people SET facets=? WHERE id=?", (json.dumps(out), person_id))
        return out

    def facets(self, person_id: str) -> dict[str, str]:
        f = self.person_facts(person_id)
        return (f or {}).get("facets") or {}

    def places(self, person_id: str) -> list[str]:
        f = self.person_facts(person_id)
        return (f or {}).get("places") or []

    def person_named(self, name: Any) -> dict[str, Any] | None:
        """The household member a name means: their first name or one of their aliases, case aside; None when no one
        or more than one does."""
        t = " ".join(str(name or "").split()).lower()
        if not t:
            return None
        hits = [p for p in self.people() if t in {str(p.get("name") or "").split(" ")[0].lower(),
                                                  *(str(a).lower() for a in p.get("aliases") or [])}]
        return hits[0] if len(hits) == 1 else None

    def calendars_ignored(self, person_id: str) -> list[str]:
        f = self.person_facts(person_id)
        return (f or {}).get("calendars_ignored") or []

    def person_for_calendar(self, title: Any) -> dict[str, str] | None:
        """The person a calendar belongs to, by the titles set in Settings › Household (exact,
        case-insensitive); None when no one claims it. A title more than one person claims (a row from before
        update_person's 409, or a race) is AMBIGUOUS_CALENDAR: nobody's own, so its events go for every viewer, the
        claimers too, since which of them it is can't be told from a title (#1234; fail closed, as #1231 does for a
        note that won't parse)."""
        t = " ".join(str(title or "").split()).lower()
        if not t:
            return None
        with self._lock:
            rows = self._db.execute("SELECT * FROM people WHERE calendars IS NOT NULL").fetchall()
        hits = [r for r in rows if t in (c.lower() for c in self._facts(r)["calendars"])]
        if len(hits) > 1:
            return dict(AMBIGUOUS_CALENDAR)
        return self._person(hits[0]) if hits else None

    def people(self) -> list[dict[str, Any]]:
        with self._lock:
            people = self._db.execute("SELECT * FROM people ORDER BY id<>?, created_at, rowid",
                                      (ADMIN_PERSON_ID,)).fetchall()
            devices = self._db.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        out = []
        for p in people:
            out.append({**self._person(p), **self._facts(p), "devices": [
                {"id": d["id"], "name": d["name"], "platform": d["platform"], "created_at": _iso(d["created_at"]),
                 "last_seen": _iso(d["last_seen"]), "revoked": d["revoked_at"] is not None,
                 "app_version": d["app_version"], "room": d["room"] if "room" in d.keys() else None,
                 "holds": json.loads(d["holds"]) if "holds" in d.keys() and d["holds"] else []}
                for d in devices if d["person_id"] == p["id"]]})
        return out

    def set_device_room(self, device_id: str, room: Any) -> str | None:
        """A device names the room it's in (jellybean#424, for follow-me rooms): 1-30 characters of plain words, or
        None to clear. Only its own row (the caller passes its own id)."""
        r = " ".join(str(room or "").split())
        if r and (len(r) > 30 or not re.fullmatch(r"[A-Za-z0-9' &-]+", r)):
            raise PairingError(400, "room must be 1-30 letters, digits, spaces, ' & or -")
        with self._lock:
            cur = self._db.execute("UPDATE devices SET room=? WHERE id=? AND revoked_at IS NULL", (r or None, device_id))
            if not cur.rowcount:
                raise PairingError(404, "no such device")
        return r or None

    def move_room(self, old: Any, new: Any) -> list[dict[str, Any]]:
        """Every live device whose room is `old` (case and spacing ignored) moves to `new`, or to no room when `new` is
        None (titan-node#1511: a rename or removal in Settings; screens follow the room). → [{id, name, room, screen}]
        as they were, for the undo and for saying which screens moved (`screen`: a household screen's own device)."""
        k = " ".join(str(old or "").split()).casefold()
        if not k:
            return []
        with self._lock:
            rows = [r for r in self._db.execute(
                "SELECT d.id, d.name, d.room, p.role FROM devices d JOIN people p ON p.id = d.person_id "
                "WHERE d.revoked_at IS NULL AND d.room IS NOT NULL").fetchall()
                if " ".join(str(r["room"]).split()).casefold() == k]
            with self._db:
                for r in rows:
                    self._db.execute("UPDATE devices SET room=? WHERE id=?", (new, r["id"]))
        return [{"id": r["id"], "name": r["name"], "room": r["room"], "screen": r["role"] == "household"} for r in rows]

    def put_device_rooms(self, rows: list[dict[str, Any]]) -> int:
        """Undo of move_room: each device back in the room it had (a device revoked since stays revoked)."""
        n = 0
        with self._lock, self._db:
            for r in rows:
                n += self._db.execute("UPDATE devices SET room=? WHERE id=? AND revoked_at IS NULL",
                                      (r.get("room"), str(r.get("id")))).rowcount
        return n

    def device_rooms(self) -> list[str]:
        """The rooms live devices name (rooms.sync takes them in as rooms)."""
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT DISTINCT room FROM devices WHERE revoked_at IS NULL AND room IS NOT NULL")]

    HOLD_KINDS = ("mail", "scan")

    def set_device_holds(self, device_id: str, holds: Any) -> list[str]:
        """The kinds of referenced item this device answers item_body for (#776: an iPhone's scan library). Only a
        person's own iPhone or iPad; [] clears it. Only its own row (the caller passes its own id)."""
        if not isinstance(holds, list) or any(str(k) not in self.HOLD_KINDS for k in holds):
            raise PairingError(400, f"holds must be a list of: {', '.join(self.HOLD_KINDS)}")
        kinds = sorted({str(k) for k in holds})
        with self._lock:
            row = self._db.execute("SELECT d.platform, p.role FROM devices d JOIN people p ON p.id = d.person_id "
                                   "WHERE d.id=? AND d.revoked_at IS NULL", (device_id,)).fetchone()
            if row is None:
                raise PairingError(404, "no such device")
            if row["platform"] not in ("ios", "ipados") or row["role"] == "household":
                raise PairingError(400, "only a person's own iPhone or iPad holds items")
            self._db.execute("UPDATE devices SET holds=? WHERE id=?", (json.dumps(kinds) if kinds else None, device_id))
        return kinds

    @staticmethod
    def _clean_name(name: Any) -> str:
        n = " ".join(str(name or "").split())
        if not n or len(n) > 40:
            raise PairingError(400, "name must be 1-40 characters")
        return n

    @staticmethod
    def _clean_role(role: Any) -> str:
        r = str(role or "").strip().lower()
        if r not in ROLES:
            raise PairingError(400, f"role must be one of: {', '.join(ROLES)}")
        return r

    def add_person(self, name: Any, role: Any) -> dict[str, str]:
        n, r = self._clean_name(name), self._clean_role(role)
        with self._lock:
            if self._db.execute("SELECT 1 FROM people WHERE lower(name)=lower(?)", (n,)).fetchone():
                raise PairingError(409, f"{n} is already in the household")
            pid = "p_" + secrets.token_hex(4)
            self._db.execute("INSERT INTO people (id, name, role, created_at) VALUES (?,?,?,?)",
                             (pid, n, r, self._now()))
        return {"id": pid, "name": n, "role": r}

    # ── calendars a device says are whose (jellybean#1037, titan-node#1234) ───────────────────────────────────
    # EventKit's calendarIdentifier is device-local: the same calendar has another id on another device. So a claim is
    # (device, calendar id) → person, made by that device, and only reads that device ran are matched against it.

    CAL_ID_RX = re.compile(r"[A-Za-z0-9._:-]{1,128}")
    MAX_DEVICE_CALENDARS = 100

    def may_claim_for(self, claimer_id: str, claimer_role: str, person_id: str) -> bool:
        """Whom a person may say a calendar on their own device is (#1268, Coordination on JEV's design): anyone in the
        household, by anyone but a shared screen. A claim only ever NARROWS who sees a calendar, unless may_widen."""
        return claimer_role != "household" and self.person(person_id) is not None

    def may_widen(self, claimer_id: str, claimer_role: str, person_id: str) -> bool:
        """Whether a claim may make visible what the title rule hides: an adult or the admin claiming for themselves or
        their own kids. A kid's claims never widen (a kid's device can list a parent's calendar: #1230's leak)."""
        if claimer_role not in ("adult", "admin"):
            return False
        if person_id == claimer_id:
            return True
        p = self.person(person_id)
        return bool(p) and p["role"] == "kid" and claimer_id in self.parents_of(person_id)

    def set_device_calendars(self, device_id: str, calendars: Any) -> dict[str, Any]:
        """This device's whole set of claims, replaced ([] clears it): [{id, title, person}] → {calendars: the accepted
        ones, refused: [{id, why: "id"|"person"|"not-allowed"}]} ("not-allowed" isn't returned today: anyone may be
        named; whether a claim widens is decided at read time, may_widen). 403 for a shared household device, 413 over
        MAX_DEVICE_CALENDARS, 404 for a revoked or unknown device."""
        if not isinstance(calendars, list) or any(not isinstance(c, dict) for c in calendars):
            raise PairingError(400, "calendars must be a list of {id, title, person}")
        if len(calendars) > self.MAX_DEVICE_CALENDARS:
            raise PairingError(413, f"at most {self.MAX_DEVICE_CALENDARS} calendars")
        with self._lock:
            row = self._db.execute("SELECT d.person_id, p.role FROM devices d JOIN people p ON p.id = d.person_id "
                                   "WHERE d.id=? AND d.revoked_at IS NULL", (device_id,)).fetchone()
        if row is None:
            raise PairingError(404, "no such device")
        if row["role"] == "household":
            raise PairingError(403, "a shared household screen claims no calendars")
        accepted: list[dict[str, str]] = []
        refused: list[dict[str, str]] = []
        seen: set[str] = set()
        for c in calendars:
            cid, pid = str(c.get("id") or ""), str(c.get("person") or "")
            title = " ".join(str(c.get("title") or "").split())[:80]
            if not self.CAL_ID_RX.fullmatch(cid) or cid in seen:
                refused.append({"id": cid[:128], "why": "id"})
                continue
            seen.add(cid)
            if self.person(pid) is None:
                refused.append({"id": cid, "why": "person"})
            elif not self.may_claim_for(row["person_id"], row["role"], pid):
                refused.append({"id": cid, "why": "not-allowed"})
            else:
                accepted.append({"id": cid, "title": title, "person": pid})
        now = self._now()
        with self._lock:
            self._db.execute("DELETE FROM device_calendars WHERE device_id=?", (device_id,))
            self._db.executemany("INSERT INTO device_calendars (device_id, cal_id, person_id, title, at) VALUES (?,?,?,?,?)",
                                 [(device_id, a["id"], a["person"], a["title"], now) for a in accepted])
        return {"calendars": accepted, "refused": refused}

    def device_calendars(self, device_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT cal_id, title, person_id FROM device_calendars WHERE device_id=? "
                                    "ORDER BY title, cal_id", (device_id,)).fetchall()
        return [{"id": r["cal_id"], "title": r["title"] or "", "person": r["person_id"]} for r in rows]

    def device_calendar_claim(self, device_id: str | None, cal_id: str | None) -> dict[str, Any] | None:
        """{owner: {id, name, role}, widens: bool} for a device's own claim, judged against the claimer's role NOW (not
        when it was saved: JEV), or None (then the title rule alone applies)."""
        owner = self.device_calendar_owner(device_id, cal_id)
        if owner is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT p.id, p.role FROM devices d JOIN people p ON p.id = d.person_id "
                                   "WHERE d.id=? AND d.revoked_at IS NULL", (device_id,)).fetchone()
        if row is None or row["role"] == "household":
            return None
        return {"owner": owner, "widens": self.may_widen(row["id"], row["role"], owner["id"])}

    def device_calendar_owner(self, device_id: str | None, cal_id: str | None) -> dict[str, str] | None:
        """The person a device's own claim gives this calendar ({id, name, role}), or None: no claim, a revoked device,
        a person no longer in the household (then the title rule applies)."""
        if not device_id or not cal_id:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT p.* FROM device_calendars c JOIN devices d ON d.id = c.device_id AND d.revoked_at IS NULL "
                "JOIN people p ON p.id = c.person_id WHERE c.device_id=? AND c.cal_id=?", (device_id, cal_id)).fetchone()
        return self._person(row) if row else None

    def parents_of(self, person_id: str) -> list[str]:
        """The person ids of someone's parents (empty when none are set)."""
        f = self.person_facts(person_id)
        return list((f or {}).get("parents") or [])

    def update_person(self, person_id: str, name: Any = None, role: Any = None, **facts: Any) -> dict[str, Any]:
        """Change a person's name, role and household facts. `facts` may hold birth_year (a year or
        None), contact ({id, name} or None), calendars (a list of titles) and parents (person ids of
        the household's adults; Zach, 10-01: "parents see and manage kids' schedules"); a key left out
        is unchanged, a None clears it."""
        with self._lock:
            cur = self.person(person_id)
            if not cur:
                raise PairingError(404, "no such person")
            n = self._clean_name(name) if name is not None else cur["name"]
            r = self._clean_role(role) if role is not None else cur["role"]
            if person_id == ADMIN_PERSON_ID and r != "admin":
                raise PairingError(400, f"{cur['name']} is the household's admin; that role can't change")
            if n.lower() != cur["name"].lower() and self._db.execute(
                    "SELECT 1 FROM people WHERE lower(name)=lower(?) AND id<>?", (n, person_id)).fetchone():
                raise PairingError(409, f"{n} is already in the household")
            sets: dict[str, Any] = {"name": n, "role": r}
            if "birth_year" in facts:
                by = facts["birth_year"]
                if by in (None, ""):
                    sets["birth_year"] = None
                else:
                    try:
                        by = int(by)
                    except (TypeError, ValueError):
                        raise PairingError(400, "birth_year must be a year") from None
                    if not 1900 <= by <= time.localtime().tm_year:
                        raise PairingError(400, "birth_year must be between 1900 and this year")
                    sets["birth_year"] = by
            if "contact" in facts:
                c = facts["contact"]
                if not c:
                    sets["contact_id"] = sets["contact_name"] = None
                elif isinstance(c, dict) and str(c.get("id") or "").strip():
                    sets["contact_id"] = str(c["id"]).strip()[:200]
                    sets["contact_name"] = " ".join(str(c.get("name") or "").split())[:80]
                else:
                    raise PairingError(400, "contact must be {id, name} or null")
            if "calendars" in facts:
                cals = facts["calendars"] or []
                if not isinstance(cals, list):
                    raise PairingError(400, "calendars must be a list of calendar titles")
                clean = []
                for c in cals:
                    t = " ".join(str(c or "").split())[:80]
                    if t and t.lower() not in (x.lower() for x in clean):
                        clean.append(t)
                taken = {}
                for row in self._db.execute("SELECT id, name, calendars FROM people WHERE id<>?", (person_id,)):
                    for t in json.loads(row["calendars"] or "[]"):
                        taken[t.lower()] = row["name"]
                clash = [t for t in clean if t.lower() in taken]
                if clash:
                    raise PairingError(409, f"The {clash[0]} calendar is already {taken[clash[0].lower()]}'s")
                sets["calendars"] = json.dumps(clean[:20])
            if "parents" in facts:
                ids = facts["parents"] or []
                if not isinstance(ids, list):
                    raise PairingError(400, "parents must be a list of person ids")
                clean_ids: list[str] = []
                for pid in ids:
                    pid = str(pid or "").strip()
                    if not pid or pid in clean_ids:
                        continue
                    if pid == person_id:
                        raise PairingError(400, "a person can't be their own parent")
                    par = self.person(pid)
                    if not par:
                        raise PairingError(404, f"no such person: {pid}")
                    if par["role"] not in ("admin", "adult"):
                        raise PairingError(400, f"{par['name']} isn't an adult, so can't be a parent here")
                    clean_ids.append(pid)
                sets["parents"] = json.dumps(clean_ids[:4])
            if "aliases" in facts:
                al = facts["aliases"] or []
                if not isinstance(al, list):
                    raise PairingError(400, "aliases must be a list of names")
                clean_al: list[str] = []
                for a in al:
                    t = " ".join(str(a or "").split())[:30]
                    if not t:
                        continue
                    if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .-]*", t):
                        raise PairingError(400, f"“{t}” isn't a name")
                    if t.lower() != n.split(" ")[0].lower() and t.lower() not in (x.lower() for x in clean_al):
                        clean_al.append(t)
                for row in self._db.execute("SELECT id, name, aliases FROM people WHERE id<>?", (person_id,)):
                    theirs = {row["name"].split(" ")[0].lower(), *(x.lower() for x in json.loads(row["aliases"] or "[]"))}
                    clash = next((t for t in clean_al if t.lower() in theirs), None)
                    if clash:
                        raise PairingError(409, f"“{clash}” already means {row['name']}")
                sets["aliases"] = json.dumps(clean_al[:10])
            if "other_parent" in facts:
                label = " ".join(str(facts["other_parent"] or "").split())[:40]
                if label and r != "kid":
                    raise PairingError(400, "other_parent is for a kid: their parent outside the household")
                sets["other_parent"] = label or None
            self._db.execute(f"UPDATE people SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                             (*sets.values(), person_id))
        return self.person_facts(person_id)

    # ── pairing ──────────────────────────────────────────────────────────────────────────────

    def start_pairing(self, person_id: str, label: Any = None, kind: Any = None) -> dict[str, Any]:
        """A code for a device to claim. kind "executor" (#1631): the claimed device is a paired executor, which reaches only
        its presence and its own inbox."""
        if kind not in (None, "", "executor"):
            raise PairingError(400, "kind is executor, or nothing for an ordinary device")
        kind = kind or None
        now = self._now()
        with self._lock:
            person = self.person(person_id)
            if not person:
                raise PairingError(404, "no such person")
            self._db.execute("DELETE FROM pair_codes WHERE expires_at < ?", (now - 86400,))
            open_n = self._db.execute("SELECT COUNT(*) FROM pair_codes WHERE used_at IS NULL AND expires_at > ?",
                                      (now,)).fetchone()[0]
            if kind == "executor" and self._db.execute(
                    "SELECT 1 FROM pair_codes WHERE person_id=? AND kind='executor' AND used_at IS NULL AND expires_at > ?",
                    (person_id, now)).fetchone():
                # #1631: one open executor code a person, so nobody fills the household's few codes with them
                raise PairingError(429, "you already have a machine pairing code open; use it or wait for it to expire")
            if open_n >= MAX_OPEN_CODES:
                raise PairingError(429, f"{MAX_OPEN_CODES} pairing codes are already open; wait for one to be used or expire")
            for _ in range(50):
                code = f"{secrets.randbelow(10**6):06d}"
                if not self._db.execute("SELECT 1 FROM pair_codes WHERE code=?", (code,)).fetchone():
                    break
            else:   # pragma: no cover - 50 collisions in a table of a few dozen rows
                raise PairingError(503, "couldn't pick a pairing code")
            lab = " ".join(str(label or "").split())[:80] or None
            self._db.execute("INSERT INTO pair_codes (code, person_id, label, created_at, expires_at, kind) "
                             "VALUES (?,?,?,?,?,?)", (code, person_id, lab, now, now + CODE_TTL_S, kind))
        urls = server_urls()
        qr = f"titan://pair?c={code}&n=titan" + "".join(f"&u={quote(u, safe='')}" for u in urls)
        return {"code": code, "expires_at": _iso(now + CODE_TTL_S), "person": person, "qr": qr}

    def drop_all_open_codes(self) -> int:
        """Delete every unused pairing code (a box's setup closing, #1188): none outlives setup. → how many."""
        with self._lock:
            return self._db.execute("DELETE FROM pair_codes WHERE used_at IS NULL").rowcount

    def drop_open_codes(self, person_id: str) -> int:
        """Delete a person's unused pairing codes (a box's setup closing, #411): → how many."""
        with self._lock:
            return self._db.execute("DELETE FROM pair_codes WHERE person_id=? AND used_at IS NULL",
                                    (person_id,)).rowcount

    def claim(self, body: Any, client_ip: str) -> dict[str, Any]:
        """POST /v1/pair/claim. Raises PairingError(400|404|410|429)."""
        if not self.limiter.admit(client_ip):
            raise PairingError(429, "too many pairing attempts; try again in a few minutes")
        try:
            return self._claim(body)
        except PairingError as exc:
            if exc.status in (400, 404, 410):
                self.limiter.failed()
            raise

    def _claim(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise PairingError(400, "expected a JSON object")
        code = str(body.get("code") or "").strip().replace(" ", "")
        if not re.fullmatch(r"\d{6}", code):
            raise PairingError(400, "code must be 6 digits")
        platform = str(body.get("platform") or "other").strip().lower()
        if platform not in PLATFORMS:
            raise PairingError(400, f"platform must be one of: {', '.join(PLATFORMS)}")
        raw_name = body.get("device_name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise PairingError(400, "device_name must be a string")
        dname = " ".join((raw_name or "").split())[:80]
        now = self._now()
        with self._lock:
            row = self._db.execute("SELECT * FROM pair_codes WHERE code=?", (code,)).fetchone()
            if not row:
                raise PairingError(404, "unknown code")
            if row["used_at"] is not None or row["expires_at"] <= now:
                raise PairingError(410, "that code has expired or was already used")
            person = self.person(row["person_id"])
            if not person:
                raise PairingError(410, "that code's person no longer exists")
            kind = row["kind"] if "kind" in row.keys() else None
            if kind == "executor" and platform != "other":
                raise PairingError(400, "an executor pairs with platform other")
            token = (EXECUTOR_TOKEN_PREFIX if kind == "executor" else "") + new_device_token()
            did = "d_" + secrets.token_hex(5)
            name = dname or row["label"] or f"{person['name']}'s device"
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute("UPDATE pair_codes SET used_at=?, device_id=? WHERE code=? AND used_at IS NULL "
                                       "AND expires_at > ?", (now, did, code, now))
                if cur.rowcount != 1:
                    self._db.execute("ROLLBACK")
                    raise PairingError(410, "that code has expired or was already used")
                self._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at, last_seen, kind) "
                                 "VALUES (?,?,?,?,?,?,?,?)",
                                 (did, person["id"], name, platform, token_sha256(token), now, now, kind or "device"))
                self._db.execute("COMMIT")
                self._seen[did] = now          # last_seen was just written
            except PairingError:
                raise
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"token": token, "device": {"id": did, "name": name}, "person": person, "servers": server_urls()}

    # ── devices and tokens ───────────────────────────────────────────────────────────────────

    def resolve_device(self, presented: str) -> Identity | None:
        """A device token → its person with that person's current role; unknown or revoked → None.
        Looked up by sha256 (the token itself is never stored), then compared in constant time. A kid's Watch
        (kind "watch", titan-node#755) resolves with via "watch" and lapses after WATCH_IDLE_S unused or WATCH_MAX_S."""
        if not presented or not _TOKEN_RX.fullmatch(presented):
            return None
        want = token_sha256(presented)
        row = self._db.execute(
            "SELECT d.id, d.name, d.platform, d.token_sha256, d.revoked_at, d.kind, d.created_at, d.last_seen, "
            "p.id AS pid, p.name AS pname, p.role "
            "FROM devices d JOIN people p ON p.id = d.person_id WHERE d.token_sha256=?", (want,)).fetchone()
        if not row or not hmac.compare_digest(row["token_sha256"], want) or row["revoked_at"] is not None:
            return None
        if row["kind"] not in DEVICE_KINDS:
            return None          # a kind this code doesn't know: fail closed, never an ordinary device
        if (row["kind"] == "executor") != presented.startswith(EXECUTOR_TOKEN_PREFIX):
            return None          # an executor's token has its prefix, and only an executor's does
        watch = row["kind"] == "watch"
        if watch:
            now, seen = self._now(), max(row["last_seen"] or 0.0, self._seen.get(row["id"], 0.0))
            if now - row["created_at"] > WATCH_MAX_S or now - (seen or row["created_at"]) > WATCH_IDLE_S:
                return None
        self._touch(row["id"])
        return Identity({"id": row["pid"], "name": row["pname"], "role": row["role"]},
                        {"id": row["id"], "name": row["name"], "platform": row["platform"]}, row["role"],
                        "watch" if watch else "executor" if row["kind"] == "executor" else "device")

    # ── a kid's Watch, paired directly (titan-node#755; the contract is on that issue) ────────────

    def watch_pair_request(self, device_name: Any) -> dict[str, Any]:
        """POST /v1/watch/pair/request: a code for the Watch to show, and the secret only it keeps (stored as sha256).
        The route rate-limits per source. Raises PairingError(400|429|503)."""
        return self._pair_request("watch", device_name)

    def _pair_request(self, kind: str, device_name: Any) -> dict[str, Any]:
        """A code to show and a poll secret (stored as sha256) for a Watch ("watch") or a shared screen ("screen")."""
        if device_name is not None and not isinstance(device_name, str):
            raise PairingError(400, "device_name must be a string")
        name = " ".join((device_name or "").split())[:80] or ("Watch" if kind == "watch" else "Apple TV")
        now = self._now()
        with self._lock:
            self._db.execute("DELETE FROM watch_pairs WHERE expires_at < ?", (now - 86400,))
            open_n = self._db.execute("SELECT COUNT(*) FROM watch_pairs WHERE closed_at IS NULL AND expires_at > ? "
                                      "AND kind=?", (now, kind)).fetchone()[0]
            if open_n >= MAX_OPEN_CODES:
                what = "Watches" if kind == "watch" else "screens"
                raise PairingError(429, f"too many {what} are waiting to be paired; try again in a few minutes")
            for _ in range(50):
                code = f"{secrets.randbelow(10**6):06d}"
                if not self._db.execute("SELECT 1 FROM watch_pairs WHERE code=?", (code,)).fetchone():
                    break
            else:   # pragma: no cover
                raise PairingError(503, "couldn't pick a pairing code")
            rid = ("wp_" if kind == "watch" else "sp_") + secrets.token_hex(8)
            secret = secrets.token_urlsafe(32)
            self._db.execute("INSERT INTO watch_pairs (request_id, code, secret_sha256, device_name, created_at, expires_at, "
                             "kind) VALUES (?,?,?,?,?,?,?)", (rid, code, token_sha256(secret), name, now, now + CODE_TTL_S, kind))
        return {"request_id": rid, "code": code, "secret": secret, "expires_at": _iso(now + CODE_TTL_S)}

    def watch_pair_confirm(self, code: Any, person_id: Any, caller: Identity) -> dict[str, Any]:
        """POST /v1/watch/pair/confirm, from a parent's iPhone: the code the Watch shows and which kid it's for. Only
        the admin or one of that kid's parents (403); only a kid (400). A code takes WATCH_CONFIRM_ATTEMPTS refusals,
        then it's spent. Raises PairingError(400|403|404|410)."""
        c = str(code or "").strip().replace(" ", "")
        if not re.fullmatch(r"\d{6}", c):
            raise PairingError(400, "code must be 6 digits")
        now = self._now()
        with self._lock:
            row = self._db.execute("SELECT * FROM watch_pairs WHERE code=? AND kind='watch'", (c,)).fetchone()
            if not row:
                raise PairingError(404, "unknown code")
            if row["closed_at"] is not None or row["person_id"] is not None or row["expires_at"] <= now:
                raise PairingError(410, "that code has expired or was already used")

            def refuse(status: int, why: str) -> PairingError:
                n = row["attempts"] + 1
                self._db.execute("UPDATE watch_pairs SET attempts=?, closed_at=? WHERE request_id=?",
                                 (n, now if n >= WATCH_CONFIRM_ATTEMPTS else None, row["request_id"]))
                return PairingError(status, why)

            kid = self._db.execute("SELECT * FROM people WHERE id=?", (str(person_id or ""),)).fetchone()
            if not kid:
                raise refuse(404, "no such person")
            if kid["role"] != "kid":
                raise refuse(400, "a Watch paired this way is only for a kid")
            parents = self.parents_of(kid["id"])
            if caller.role != "admin" and caller.person_id not in parents:
                raise refuse(403, "only the admin or one of their parents can pair a kid's Watch")
            self._db.execute("UPDATE watch_pairs SET person_id=?, confirmed_by=? WHERE request_id=?",
                             (kid["id"], caller.person_id, row["request_id"]))
        return {"request_id": row["request_id"], "kid_first_name": (kid["name"] or "").split(" ")[0]}

    def watch_pair_poll(self, request_id: Any, secret: Any) -> dict[str, Any] | None:
        """GET /v1/watch/pair/request/{id} with the Watch's secret: None while it waits for a parent; once confirmed,
        the new token, once (the request then closes). Raises PairingError(404|410)."""
        rid, sec = str(request_id or ""), str(secret or "")
        now = self._now()
        with self._lock:
            row = self._db.execute("SELECT * FROM watch_pairs WHERE request_id=? AND kind='watch'", (rid,)).fetchone()
            if not row or not sec or not hmac.compare_digest(row["secret_sha256"], token_sha256(sec)):
                raise PairingError(404, "unknown request")
            if row["closed_at"] is not None or row["expires_at"] <= now:
                raise PairingError(410, "that request has expired or was already used")
            if row["person_id"] is None:
                return None
            kid = self.person(row["person_id"])
            if not kid or kid["role"] != "kid":
                raise PairingError(410, "that request's kid no longer exists")
            token, did = new_device_token(), "d_" + secrets.token_hex(5)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute("UPDATE watch_pairs SET closed_at=?, device_id=? WHERE request_id=? AND closed_at IS NULL",
                                       (now, did, rid))
                if cur.rowcount != 1:
                    self._db.execute("ROLLBACK")
                    raise PairingError(410, "that request has expired or was already used")
                self._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at, last_seen, kind) "
                                 "VALUES (?,?,?,?,?,?,?,'watch')", (did, kid["id"], row["device_name"], "watchos",
                                                                    token_sha256(token), now, now))
                self._db.execute("COMMIT")
                self._seen[did] = now
            except PairingError:
                raise
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"token": token, "device": {"id": did, "name": row["device_name"]},
                "kid_first_name": (kid["name"] or "").split(" ")[0], "servers": server_urls()}

    # ── a shared screen (an Apple TV), paired with no typing on the remote (#411, jellybean#434) ────────────────
    # The Watch's flow and hardening (10 minutes, single use, the secret hashed, a code spent after 3 refusals); only
    # the admin confirms, and the TV collects a household-role token for a device of kind "tv". The contract is on #411.

    def screen_pair_request(self, device_name: Any) -> dict[str, Any]:
        """POST /v1/screen/pair/request: the code and QR the TV shows, and the secret only it keeps."""
        out = self._pair_request("screen", device_name)
        return {**out, "qr": f"titan://pair-screen?c={out['code']}&n=titan"}

    def screen_pair_confirm(self, code: Any, caller: Identity, name: Any = None, person_id: Any = None,
                            room: Any = None, device_name: str | None = None) -> dict[str, Any]:
        """POST /v1/screen/pair/confirm, the admin only: the code the TV shows, and either a name for a new screen or
        the household-role person of a screen being paired again (a TV that was reset). Raises
        PairingError(400|403|404|409|410)."""
        if caller.role != "admin":
            raise PairingError(403, "only the admin can add a screen")
        c = str(code or "").strip().replace(" ", "")
        if not re.fullmatch(r"\d{6}", c):
            raise PairingError(400, "code must be 6 digits")
        if room is not None and not isinstance(room, str):
            raise PairingError(400, "room must be a string")
        rm = " ".join((room or "").split())[:60] or None
        pid = str(person_id or "").strip() or None
        if pid is None:
            n = self._clean_name(name)
            if self._db.execute("SELECT 1 FROM people WHERE lower(name)=lower(?)", (n,)).fetchone():
                raise PairingError(409, f"{n} is already in the household; pair it again by choosing it")
        now = self._now()
        with self._lock:
            row = self._db.execute("SELECT * FROM watch_pairs WHERE code=? AND kind='screen'", (c,)).fetchone()
            if not row:
                raise PairingError(404, "unknown code")
            if row["closed_at"] is not None or row["confirmed_by"] is not None or row["expires_at"] <= now:
                raise PairingError(410, "that code has expired or was already used")
            if pid is not None:
                p = self._db.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
                if not p or p["role"] != "household":
                    n_ = row["attempts"] + 1
                    self._db.execute("UPDATE watch_pairs SET attempts=?, closed_at=? WHERE request_id=?",
                                     (n_, now if n_ >= WATCH_CONFIRM_ATTEMPTS else None, row["request_id"]))
                    raise PairingError(400, "that isn't one of the household's screens")
                n = p["name"]
            dev = " ".join(str(device_name or "").split())[:80] or None
            self._db.execute("UPDATE watch_pairs SET person_id=?, screen_name=?, room=?, confirmed_by=?, confirmed_device=? "
                             "WHERE request_id=?", (pid, n, rm, caller.person_id, dev, row["request_id"]))
        return {"request_id": row["request_id"], "name": n}

    def screen_pair_poll(self, request_id: Any, secret: Any) -> dict[str, Any] | None:
        """GET /v1/screen/pair/request/{id} with the TV's secret: None while it waits for the admin; once confirmed, a
        household-role token for a new device of kind "tv", once (the request then closes). Raises PairingError(404|410)."""
        rid, sec = str(request_id or ""), str(secret or "")
        now = self._now()
        with self._lock:
            row = self._db.execute("SELECT * FROM watch_pairs WHERE request_id=? AND kind='screen'", (rid,)).fetchone()
            if not row or not sec or not hmac.compare_digest(row["secret_sha256"], token_sha256(sec)):
                raise PairingError(404, "unknown request")
            if row["closed_at"] is not None or row["expires_at"] <= now:
                raise PairingError(410, "that request has expired or was already used")
            if row["confirmed_by"] is None:
                return None
            admin = self.person(row["confirmed_by"])
            if not admin or admin["role"] != "admin":
                raise PairingError(410, "the person who confirmed it is no longer the admin")
            token, did = new_device_token(), "d_" + secrets.token_hex(5)
            self._db.execute("BEGIN IMMEDIATE")
            try:
                cur = self._db.execute("UPDATE watch_pairs SET closed_at=?, device_id=? WHERE request_id=? AND closed_at IS NULL",
                                       (now, did, rid))
                if cur.rowcount != 1:
                    raise PairingError(410, "that request has expired or was already used")
                if row["person_id"]:
                    p = self._db.execute("SELECT * FROM people WHERE id=?", (row["person_id"],)).fetchone()
                    if not p or p["role"] != "household":
                        raise PairingError(410, "that screen is no longer in the household")
                    person = self._person(p)
                else:
                    if self._db.execute("SELECT 1 FROM people WHERE lower(name)=lower(?)", (row["screen_name"],)).fetchone():
                        raise PairingError(410, f"{row['screen_name']} was added meanwhile; pair again")
                    person = {"id": "p_" + secrets.token_hex(4), "name": row["screen_name"], "role": "household"}
                    self._db.execute("INSERT INTO people (id, name, role, created_at) VALUES (?,?,?,?)",
                                     (person["id"], person["name"], "household", now))
                self._db.execute("INSERT INTO devices (id, person_id, name, platform, token_sha256, created_at, last_seen, "
                                 "kind, room) VALUES (?,?,?,?,?,?,?,'tv',?)",
                                 (did, person["id"], person["name"], "tvos", token_sha256(token), now, now, row["room"]))
                self._db.execute("COMMIT")
                self._seen[did] = now
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {"token": token, "device": {"id": did, "name": person["name"], "kind": "tv", "room": row["room"]},
                "person": person,
                "confirmed_by": {"first_name": (admin["name"] or "").split(" ")[0], "device_name": row["confirmed_device"]},
                "servers": server_urls()}

    def watch_devices_for(self, caller: Identity) -> list[dict[str, Any]]:
        """GET /v1/watch/devices: the kids' Watches this caller may revoke (the admin: every one; a parent: their
        kids'). Name, the kid's first name and dates only."""
        rows = self._db.execute("SELECT d.id, d.name, d.created_at, d.last_seen, p.id AS pid, p.name AS pname FROM devices d "
                                "JOIN people p ON p.id = d.person_id WHERE d.kind='watch' AND d.revoked_at IS NULL "
                                "ORDER BY d.created_at").fetchall()
        return [{"id": r["id"], "name": r["name"], "kid_first_name": (r["pname"] or "").split(" ")[0],
                 "paired_at": _iso(r["created_at"]), "last_seen": _iso(r["last_seen"]) if r["last_seen"] else None}
                for r in rows if caller.role == "admin" or caller.person_id in self.parents_of(r["pid"])]

    def may_revoke(self, caller: Identity, device_id: str) -> bool:
        """DELETE /v1/devices/{id}: the admin, the device itself, an adult for their own other devices (#1079), or
        (titan-node#755) a parent of the person whose kid's Watch it is."""
        if caller.role == "admin" or (caller.device or {}).get("id") == device_id:
            return True
        row = self._db.execute("SELECT person_id, kind FROM devices WHERE id=?", (device_id,)).fetchone()
        if not row:
            return False
        # an adult signs out one of their own other devices (a lost iPhone, from their Mac; #1079). Kids go through a
        # parent, and the device making the request signs itself out the usual way.
        if caller.role == "adult" and row["person_id"] == caller.person_id:
            return True
        return row["kind"] == "watch" and caller.person_id in self.parents_of(row["person_id"])

    def _touch(self, device_id: str) -> None:
        now = self._now()
        if now - self._seen.get(device_id, 0.0) < LAST_SEEN_EVERY_S:
            return
        self._seen[device_id] = now
        try:
            with self._lock:
                self._db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))
        except sqlite3.Error:
            pass

    def set_device_version(self, device_id: str, version: str | None) -> None:
        """The app version a device reports (X-Client-Version), written only when it changes."""
        if not version or self._versions.get(device_id) == version:
            return
        self._versions[device_id] = version
        try:
            with self._lock:
                self._db.execute("UPDATE devices SET app_version=? WHERE id=?", (version, device_id))
        except sqlite3.Error:
            pass

    def revoke_device(self, device_id: str) -> bool:
        with self._lock:
            self._db.execute("DELETE FROM device_calendars WHERE device_id=?", (device_id,))   # its claims go with it
            cur = self._db.execute("UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                   (self._now(), device_id))
            if cur.rowcount:
                return True
            return self._db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone() is not None

    # ── Macs (cross-device: scripts/cross_device.py) ─────────────────────────────────────────
    # A Mac belongs to the person its token resolves to; rows are keyed by (person, device_key), so
    # nobody can claim another person's Mac by sending its device_key.

    def record_presence(self, person_id: str, device_id: str | None, p: dict[str, Any],
                        *, touch_only: bool = False, kind: str = "mac") -> None:
        """Upsert a Mac's last presence (p is validated by cross_device.clean_presence). touch_only:
        a long-poll arrived; create the row with just its name if it's new, else keep what's there."""
        now = self._now()
        with self._lock:
            # jellybean#413: each source's level and since when (a level that didn't change keeps its time).
            src_json = None
            if p.get("sources") is not None and not touch_only:
                prev_row = self._db.execute("SELECT sources FROM mac_presence WHERE person_id=? AND device_key=?",
                                            (person_id, p["device_key"])).fetchone()
                try:
                    prev = json.loads(prev_row["sources"]) if prev_row and prev_row["sources"] else {}
                except ValueError:
                    prev = {}
                src_json = json.dumps({k: {"level": v, "since": (prev.get(k) or {}).get("since")
                                           if (prev.get(k) or {}).get("level") == v else now}
                                       for k, v in p["sources"].items()})
            if touch_only:
                self._db.execute(
                    "INSERT INTO mac_presence (person_id, device_key, device_id, name, updated_at, kind) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(person_id, device_key) DO UPDATE SET name=excluded.name, device_id=excluded.device_id",
                    (person_id, p["device_key"], device_id, p["name"], now, kind))
                return
            self._db.execute(
                "INSERT INTO mac_presence (person_id, device_key, device_id, name, lan_ip, mac_address, wake_port, "
                "on_tailnet, awake, updated_at, battery_pct, on_ac, disk_free_gb, app_version, device_tools, sources, kind) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(person_id, device_key) DO UPDATE SET "
                "device_id=excluded.device_id, name=excluded.name, lan_ip=excluded.lan_ip, mac_address=excluded.mac_address, "
                "wake_port=excluded.wake_port, on_tailnet=excluded.on_tailnet, awake=excluded.awake, updated_at=excluded.updated_at, "
                "battery_pct=excluded.battery_pct, on_ac=excluded.on_ac, disk_free_gb=excluded.disk_free_gb, "
                "app_version=COALESCE(excluded.app_version, mac_presence.app_version), "
                "device_tools=COALESCE(excluded.device_tools, mac_presence.device_tools), "
                "sources=COALESCE(excluded.sources, mac_presence.sources)",   # a row's kind is set once, never changed
                (person_id, p["device_key"], device_id, p["name"], p.get("lan_ip"), p.get("mac_address"),
                 p.get("wake_port"), int(bool(p.get("on_tailnet"))), int(bool(p.get("awake", True))), now,
                 p.get("battery_pct"), None if p.get("on_ac") is None else int(bool(p["on_ac"])), p.get("disk_free_gb"),
                 p.get("app_version"), None if p.get("device_tools") is None else json.dumps(p["device_tools"]),
                 src_json, kind))

    def presence_kind(self, person_id: str, device_key: str) -> str | None:
        """A presence row's kind ("mac" for one from before #1631), or None when there's no row."""
        r = self._db.execute("SELECT COALESCE(kind, 'mac') FROM mac_presence WHERE person_id=? AND device_key=?",
                             (person_id, device_key)).fetchone()
        return r[0] if r else None

    def macs_for(self, person_id: str, kind: str = "mac") -> list[dict[str, Any]]:
        """A person's Macs, most recently heard from first. A revoked device's Mac is left out. Only kind "mac" (a row
        from before #1631 has none and is a Mac): a paired executor is never a Mac, so Mac tools never route to one.
        kind="executor": their executors instead."""
        rows = self._db.execute(
            "SELECT m.* FROM mac_presence m LEFT JOIN devices d ON d.id = m.device_id "
            "WHERE m.person_id=? AND (m.device_id IS NULL OR d.revoked_at IS NULL) AND COALESCE(m.kind, 'mac') = ? "
            "ORDER BY m.updated_at DESC", (person_id, kind)).fetchall()
        return [{"person_id": r["person_id"], "device_key": r["device_key"], "device_id": r["device_id"],
                 "name": r["name"], "lan_ip": r["lan_ip"], "mac_address": r["mac_address"], "wake_port": r["wake_port"],
                 "on_tailnet": bool(r["on_tailnet"]), "awake": bool(r["awake"]), "updated_at": _iso(r["updated_at"]),
                 "battery_pct": r["battery_pct"], "on_ac": None if r["on_ac"] is None else bool(r["on_ac"]),
                 "disk_free_gb": r["disk_free_gb"], "app_version": r["app_version"],
                 "device_tools": json.loads(r["device_tools"]) if r["device_tools"] else None,
                 "sources": _sources_of(r)}
                for r in rows]

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:  # noqa: BLE001
            pass


_store: HouseholdStore | None = None
_store_lock = threading.Lock()


def resolve(presented: str, master_token: str) -> Identity | None:
    """The identity behind an X-Agent-Token: the master token → Zach (admin), even when the store
    can't be opened; a device token → its person; anything else (unknown, revoked) → None."""
    if not presented:
        return None
    if master_token and hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                                            master_token.encode("utf-8", "surrogateescape")):
        try:
            p = get_store().person(ADMIN_PERSON_ID)
        except Exception:  # noqa: BLE001 - the master token must work without the store
            p = None
        return owner_identity("master", p["name"] if p else ADMIN_PERSON_NAME)
    eng = _engineering_token()
    if eng and len(eng) >= 32 and hmac.compare_digest(presented.encode("utf-8", "surrogateescape"),
                                                      eng.encode("utf-8", "surrogateescape")):
        return engineering_identity()
    try:
        return get_store().resolve_device(presented)
    except Exception:  # noqa: BLE001
        return None


def get_store() -> HouseholdStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = HouseholdStore()
    return _store


def reset_store(store: HouseholdStore | None = None) -> None:
    """Tests: swap the store (None: the next get_store() opens HOUSEHOLD_DB afresh)."""
    global _store
    with _store_lock:
        _store = store


# ════════════════════════════════════════════════════════════════════════════════════════════
# Where titan can be reached (the pairing QR's URLs, the claim's `servers`)
# ════════════════════════════════════════════════════════════════════════════════════════════

_urls_cache: dict[str, Any] = {"at": 0.0, "urls": None}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return ""


def lan_addresses() -> list[str]:
    """titan's own private IPv4 addresses on the home networks (10.x first, then 192.168.x):
    the ones a phone on the Wi-Fi can reach and mDNS advertises. Empty where `ip` doesn't exist."""
    out = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    addrs = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", out)
    home = [a for a in addrs if a.startswith("10.") and not a.startswith("10.255.")] + \
           [a for a in addrs if a.startswith("192.168.")]
    return list(dict.fromkeys(home))


# The mDNS host name titan's Advertiser registered ("titan-node.local"), while it's advertising; None otherwise. Only
# what zeroconf already advertises (jellybean#764): a kid's Watch, which can't browse Bonjour, reaches titan by this
# name on the home network after titan's IP changes. Never a new advertisement or port.
ADVERTISED_HOST: str | None = None


def _compute_urls(port: int) -> list[str]:
    urls = [f"http://{a}:{port}" for a in lan_addresses()]
    if ADVERTISED_HOST:   # after the LAN IPs, before the tailnet: a client trying in order loses nothing
        urls.append(f"http://{ADVERTISED_HOST}:{port}")
    ts_ip = next((l.strip() for l in _run(["tailscale", "ip", "-4"]).splitlines() if l.strip()), "")
    if ts_ip:
        urls.append(f"http://{ts_ip}:{port}")
    try:
        import json
        dns = str(json.loads(_run(["tailscale", "status", "--json"]) or "{}").get("Self", {}).get("DNSName") or "")
    except ValueError:
        dns = ""
    if dns.strip("."):
        urls.append(f"http://{dns.strip('.')}:{port}")
    return list(dict.fromkeys(urls))


def server_urls() -> list[str]:
    """TITAN_PAIR_URLS (comma-separated) if set; else the LAN, tailnet IP and MagicDNS URLs worked
    out on titan (cached 5 minutes); else the contract's defaults."""
    env = os.environ.get("TITAN_PAIR_URLS", "").strip()
    if env:
        return [u.strip().rstrip("/") for u in env.split(",") if u.strip()]
    now = time.monotonic()
    if _urls_cache["urls"] is None or now - _urls_cache["at"] > 300:
        port = int(os.environ.get("AGENT_API_PORT", str(AGENT_API_PORT)) or AGENT_API_PORT)
        _urls_cache["urls"] = _compute_urls(port) or list(DEFAULT_SERVER_URLS)
        _urls_cache["at"] = now
    return list(_urls_cache["urls"])


# ════════════════════════════════════════════════════════════════════════════════════════════
# Discovery: _titan._tcp.local. over mDNS (python-zeroconf), from the API server process
# ════════════════════════════════════════════════════════════════════════════════════════════

SERVICE_TYPE = "_titan._tcp.local."


def service_info_kwargs(port: int, addresses: list[str], host: str) -> dict[str, Any]:
    """What titan advertises: instance "titan", the API port, TXT v=1, on its LAN addresses."""
    import socket
    return {"type_": SERVICE_TYPE, "name": f"titan.{SERVICE_TYPE}", "port": port,
            "properties": {"v": "1"}, "server": f"{host.split('.')[0]}.local.",
            "addresses": [socket.inet_aton(a) for a in addresses]}


def _set_advertised_host(host: str | None) -> None:
    """The name zeroconf registered (or None when it stops), and server_urls works the list out again."""
    global ADVERTISED_HOST
    if host != ADVERTISED_HOST:
        ADVERTISED_HOST = host
        _urls_cache["urls"] = None


@dataclass
class Advertiser:
    """Registers the service on start(), unregisters and closes on stop(). Sync zeroconf calls
    run on their own thread (newer zeroconf refuses sync calls on a running event loop)."""
    port: int
    zc: Any = None
    info: Any = None
    status: str = "off"
    addresses: list[str] = field(default_factory=list)

    def start(self) -> str:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            self.status = "unavailable: python-zeroconf is not installed (apt install python3-zeroconf)"
            return self.status
        import socket
        self.addresses = lan_addresses()
        if not self.addresses:
            self.status = "off: no LAN address to advertise on"
            return self.status
        try:
            kw = service_info_kwargs(self.port, self.addresses, socket.gethostname())
            self.info = ServiceInfo(**kw)
            self.zc = Zeroconf(interfaces=self.addresses)
            self.zc.register_service(self.info)
            self.status = f"advertising {SERVICE_TYPE} on {', '.join(self.addresses)}:{self.port}"
            _set_advertised_host(str(kw.get("server") or "").rstrip(".") or None)   # the A record it just published
        except Exception as exc:  # noqa: BLE001 - discovery must never stop the API
            self.status = f"failed: {type(exc).__name__}: {str(exc)[:160]}"
            self.stop()
        return self.status

    def stop(self) -> None:
        zc, info = self.zc, self.info
        self.zc = self.info = None
        _set_advertised_host(None)
        if zc is None:
            return
        try:
            if info is not None:
                zc.unregister_service(info)
        except Exception:  # noqa: BLE001
            pass
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass
        if self.status.startswith("advertising"):
            self.status = "stopped"
