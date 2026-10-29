"""Jar Appliance Node Server Runner."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add core runtime to sys.path
CORE_DIR = Path(__file__).resolve().parent / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

# Base Jar Directories
JAR_HOME = Path(os.environ.get("JAR_HOME", Path.home() / ".jar")).resolve()
CONFIG_DIR = JAR_HOME / "config"
DATA_DIR = JAR_HOME / "data"
LOGS_DIR = JAR_HOME / "logs"
MEMORY_DIR = JAR_HOME / "memory"
TMP_DIR = JAR_HOME / "tmp"

for d in (CONFIG_DIR, DATA_DIR, LOGS_DIR, MEMORY_DIR, TMP_DIR, CONFIG_DIR / "box-setup", DATA_DIR / "world"):
    d.mkdir(parents=True, exist_ok=True)

# 1. Base Box & Setup
os.environ.setdefault("BOX_SETUP", "1")
os.environ.setdefault("BOX_SETUP_DIR", str(CONFIG_DIR / "box-setup"))
os.environ.setdefault("AGENT_API_PORT", os.environ.get("PORT", "7770"))
os.environ.setdefault("OPS_AGENT_API_PORT", os.environ.get("AGENT_API_PORT", "7770"))

# 2. Database & Storage Redirection (Relocates all /ai/ paths to $JAR_HOME)
os.environ.setdefault("HOUSEHOLD_DB", str(DATA_DIR / "household.db"))
os.environ.setdefault("CONNECTORS_DIR", str(DATA_DIR))
os.environ.setdefault("WORLD_DIR", str(DATA_DIR / "world"))
os.environ.setdefault("WORLD_DB", str(DATA_DIR / "world.db"))
os.environ.setdefault("HOUSEHOLD_PREFS", str(DATA_DIR / "household.json"))
os.environ.setdefault("FENCES_DB", str(DATA_DIR / "fences.db"))
os.environ.setdefault("ROOMS_DB", str(DATA_DIR / "rooms.db"))
os.environ.setdefault("OUR_YEAR_DB", str(DATA_DIR / "our_year.db"))
os.environ.setdefault("HELD_ACTIONS_DB", str(DATA_DIR / "held_actions.db"))
os.environ.setdefault("TASKS_DB", str(DATA_DIR / "tasks.db"))
os.environ.setdefault("TODOS_DB", str(DATA_DIR / "todos.db"))
os.environ.setdefault("ROUTINES_DB", str(DATA_DIR / "routines.db"))
os.environ.setdefault("SCHEDULED_DB", str(DATA_DIR / "scheduled.db"))
os.environ.setdefault("UPLOADS_DB", str(DATA_DIR / "uploads.db"))
os.environ.setdefault("UPLOADS_DIR", str(TMP_DIR / "uploads"))
os.environ.setdefault("IMAGES_DB", str(DATA_DIR / "images.db"))
os.environ.setdefault("GENERATED_DIR", str(TMP_DIR))
os.environ.setdefault("PLACES_DB", str(DATA_DIR / "places.db"))
os.environ.setdefault("PROACTIVE_DB", str(DATA_DIR / "proactive.db"))
os.environ.setdefault("REFLECTION_DB", str(DATA_DIR / "reflection.db"))
os.environ.setdefault("SURFACES_DB", str(DATA_DIR / "surfaces.db"))
os.environ.setdefault("REFERENCES_DB", str(DATA_DIR / "references.db"))
os.environ.setdefault("LINKS_DB", str(DATA_DIR / "links.db"))
os.environ.setdefault("HANDOFF_DB", str(DATA_DIR / "handoff.db"))
os.environ.setdefault("FAMILY_MEETING_DB", str(DATA_DIR / "family_meeting.db"))
os.environ.setdefault("MAIL_BILLS_DB", str(DATA_DIR / "mail_bills.db"))
os.environ.setdefault("MAIL_TRIAGE_DB", str(DATA_DIR / "mail_triage.db"))
os.environ.setdefault("CONNECTORS_LEDGER", str(DATA_DIR / "ledger.db"))
os.environ.setdefault("TV_UNLOCK_DB", str(DATA_DIR / "tv_unlock.db"))
os.environ.setdefault("REVIEW_DB", str(DATA_DIR / "scan_review.db"))
os.environ.setdefault("DEVICE_HEALTH_STATE", str(DATA_DIR / "device_health.json"))
os.environ.setdefault("NOW_FEED_CACHE", str(DATA_DIR / "now_feed_cache.json"))
os.environ.setdefault("KB_QUESTIONS_CACHE", str(DATA_DIR / "kb_questions.json"))
os.environ.setdefault("SERVER_IDENTITY_KEY", str(CONFIG_DIR / "server-identity.key"))

# 3. Logs & Caches
os.environ.setdefault("AGENT_TURN_LOG", str(LOGS_DIR / "agent-turns.jsonl"))
os.environ.setdefault("ITEMS_LATENCY_LOG", str(LOGS_DIR / "items-latency.jsonl"))

import box_setup
import household_identity as hh


def get_lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def get_setup_urls(port: int | None = None) -> tuple[str, str]:
    port = port or int(os.environ.get("AGENT_API_PORT", "7770"))
    key = box_setup.ensure_key()
    box_setup.rearm()
    lan_ip = get_lan_ip()
    local_url = f"http://localhost:{port}/setup#k={key}"
    net_url = f"http://{lan_ip}:{port}/setup#k={key}"
    return local_url, net_url


def print_banner(port: int) -> None:
    store = hh.get_store()
    lan_ip = get_lan_ip()
    print("=" * 64)
    print("            🏺   JAR HOUSEHOLD APPLIANCE NODE   🏺")
    print("=" * 64)
    print(f"Data root:        {JAR_HOME}")
    print(f"Server Port:      {port}")
    print(f"Local URL:        http://localhost:{port}")
    print(f"Network URL:      http://{lan_ip}:{port}")

    if box_setup.needs_setup(store):
        local_url, net_url = get_setup_urls(port)
        print("-" * 64)
        print("  ⚡ FIRST-TIME SETUP REQUIRED ⚡")
        print("  Open this link in your browser or scan with your phone:")
        print(f"\n    👉  {net_url}\n")
        print("-" * 64)
    else:
        print("-" * 64)
        print("  ✅ Node is configured and active.")
        print("-" * 64)


def run_server() -> None:
    port = int(os.environ.get("AGENT_API_PORT", "7770"))
    host = os.environ.get("AGENT_API_HOST", "0.0.0.0")

    print_banner(port)

    import uvicorn
    import agent_api_server

    uvicorn.run(
        agent_api_server.app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
