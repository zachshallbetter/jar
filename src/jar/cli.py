"""Jar Command Line Interface."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

JAR_HOME = Path(os.environ.get("JAR_HOME", Path.home() / ".jar")).resolve()
PID_FILE = JAR_HOME / "jar.pid"
LOG_FILE = JAR_HOME / "logs" / "node.log"


def cmd_run() -> None:
    from jar.server import run_server
    run_server()


def cmd_start() -> None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"Jar Node is already running (PID {pid}).")
            return
        except OSError:
            PID_FILE.unlink(missing_ok=True)

    print("Starting Jar Node daemon in background...")
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Spawn daemon process using current python interpreter
    cmd = [sys.executable, "-m", "jar.cli", "run"]
    with open(LOG_FILE, "a") as out:
        p = subprocess.Popen(
            cmd,
            stdout=out,
            stderr=out,
            start_new_session=True,
            env=os.environ.copy(),
        )
    PID_FILE.write_text(str(p.pid))
    time.sleep(1.5)
    cmd_status()


def cmd_stop() -> None:
    if not PID_FILE.exists():
        print("Jar Node is not running.")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        print(f"Stopping Jar Node (PID {pid})...")
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
        print("Stopped.")
    except Exception as exc:
        print(f"Error stopping node: {exc}")
    finally:
        PID_FILE.unlink(missing_ok=True)


def cmd_status() -> None:
    port = os.environ.get("AGENT_API_PORT", "7770")
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            print(f"✅ Jar Node is running (PID {pid}) on port {port}.")
            import urllib.request
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as resp:
                    print(f"Health: {resp.read().decode('utf-8')}")
            except Exception:
                print("Health: Node starting up or endpoint initializing...")
            return
        except OSError:
            PID_FILE.unlink(missing_ok=True)

    print("⭕ Jar Node is stopped.")


def cmd_url() -> None:
    from jar.server import get_setup_urls
    local_url, net_url = get_setup_urls()
    print("=" * 64)
    print("  🏺 JAR ONBOARDING SETUP URLS 🏺")
    print("=" * 64)
    print(f"Local:   {local_url}")
    print(f"Network: {net_url}")
    print("=" * 64)


def cmd_profile() -> None:
    from jar.hardware import inspect_all
    data = inspect_all()
    plat = data["platform"]
    mem = data["memory"]
    acc = data["accelerators"]
    rec = data["recommendation"]

    print("=" * 64)
    print("         🏺 JAR HARDWARE TELEMETRY & MODEL FIT 🏺")
    print("=" * 64)
    print(f"OS:           {plat['platform_system']} ({plat['arch']})")
    print(f"RAM:          {mem['ram_gb']} GB")
    print("Accelerators:")
    for dev in acc["devices"]:
        vram_str = f" ({dev['vram_gb']} GB VRAM)" if "vram_gb" in dev else ""
        print(f"  • {dev['name']}{vram_str}")
    print("-" * 64)
    print(f"Profile Tier: {rec['tier'].upper()} ({rec['backend']})")
    print(f"Model:        {rec['recommended_model']}")
    print(f"Context:      {rec['recommended_context']} tokens")
    print(f"Note:         {rec['description']}")
    print("=" * 64)


def cmd_logs() -> None:
    if not LOG_FILE.exists():
        print(f"No log file found at {LOG_FILE}.")
        return
    try:
        subprocess.run(["tail", "-f", str(LOG_FILE)])
    except KeyboardInterrupt:
        pass


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "help"

    if cmd == "run":
        cmd_run()
    elif cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "url":
        cmd_url()
    elif cmd == "profile":
        cmd_profile()
    elif cmd == "logs":
        cmd_logs()
    else:
        print("Jar: Sovereign Household AI Appliance")
        print("\nUsage:")
        print("  jar run       Run the server in the foreground")
        print("  jar start     Start the daemon in the background")
        print("  jar stop      Stop the background daemon")
        print("  jar status    Check service health and PID")
        print("  jar url       Display current /setup URL and pairing key")
        print("  jar profile   Inspect host hardware and recommended model")
        print("  jar logs      Stream live operational logs")
        sys.exit(0 if cmd == "help" else 1)


if __name__ == "__main__":
    main()

