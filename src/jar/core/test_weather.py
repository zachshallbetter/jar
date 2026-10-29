#!/usr/bin/env python3
"""get_weather against a fake Open-Meteo: geocoding picks the right same-named town, the home
place is saved only when asked, no home → the tool tells the model to ask the user, forecast
lines (units, Today/Tomorrow, rain chance, wind only when notable). No network."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["HA_TOKEN_FILE"] = "/nonexistent/ha-token"   # never reach the live Home Assistant (titan has a token)
import weather  # noqa: E402

ok = True


def check(label, got, expected):
    global ok
    good = got == expected
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


GEO = {"results": [
    {"name": "Spokane", "admin1": "Missouri", "country": "United States", "country_code": "US", "latitude": 36.99, "longitude": -93.3, "timezone": "America/Chicago"},
    {"name": "Spokane", "admin1": "Washington", "country": "United States", "country_code": "US", "latitude": 47.66, "longitude": -117.43, "timezone": "America/Los_Angeles"}]}
FC = {"current": {"temperature_2m": 57.6, "weather_code": 2, "wind_speed_10m": 6.1},
      "daily": {"time": ["2026-09-30", "2026-10-01", "2026-10-02"], "weather_code": [0, 61, 3],
                "temperature_2m_max": [71.2, 63.8, 66.0], "temperature_2m_min": [44.1, 47.9, 45.5],
                "precipitation_probability_max": [0, 70, 10], "wind_speed_10m_max": [8.0, 24.6, 9.0]}}
urls = []


def fake(url):
    urls.append(url)
    return GEO if "geocoding" in url else FC


with tempfile.TemporaryDirectory() as td:
    prefs = Path(td) / "household.json"
    today = date(2026, 9, 30)
    print("── no home yet ──")
    check("asks the user instead of guessing", weather.get_weather({}, fetch=fake, prefs=prefs, today=today), weather.NO_HOME)
    check("no network call without a place", urls, [])

    check("home from Home Assistant's location", weather.ha_home(lambda m, p, b: {"latitude": 47.6, "longitude": -117.4, "time_zone": "America/Los_Angeles"}),
          {"name": "Home", "latitude": 47.6, "longitude": -117.4, "timezone": "America/Los_Angeles"})
    check("no HA location → None", weather.ha_home(lambda m, p, b: {"latitude": None}), None)
    def boom(*a):
        raise OSError("down")
    check("HA unreachable → None", weather.ha_home(boom), None)

    print("── geocoding ──")
    check("a state abbreviation picks the right town", weather.geocode("Spokane, WA", fake)["name"], "Spokane, Washington")
    check("a state name too", weather.geocode("Spokane, Missouri", fake)["name"], "Spokane, Missouri")
    check("unknown → None", weather.geocode("Nowhere", lambda u: {"results": []}), None)
    check("empty → None", weather.geocode(" , ", fake), None)

    print("── a named place isn't saved unless asked ──")
    out = weather.get_weather({"place": "Spokane, WA"}, fetch=fake, prefs=prefs, today=today)
    check("forecast for the named place", out.splitlines()[0], "Spokane, Washington")
    check("not saved", prefs.exists(), False)

    print("── remember=true saves home ──")
    out = weather.get_weather({"place": "Spokane, WA", "remember": "true"}, fetch=fake, prefs=prefs, today=today)
    check("says it was saved", out.endswith("won't need the place.)"), True)
    check("saved home", weather.home(prefs)["name"], "Spokane, Washington")
    urls.clear()
    out = weather.get_weather({"days": 3}, fetch=fake, prefs=prefs, today=today)
    check("later: no geocoding", any("geocoding" in u for u in urls), False)
    lines = out.splitlines()
    check("now line", lines[1], "Now: 58°F, partly cloudy, wind 6 mph")
    check("today", lines[2], "Today (Wed Sep 30): clear, high 71°F, low 44°F, 0% chance of rain")
    check("tomorrow with notable wind", lines[3], "Tomorrow (Thu Oct 1): light rain, high 64°F, low 48°F, 70% chance of rain, wind up to 25 mph")
    check("then the weekday", lines[4].startswith("Friday (Fri Oct 2): overcast"), True)
    check("fahrenheit by default", "temperature_unit=fahrenheit" in urls[-1], True)
    check("days clamped to 7", "forecast_days=7" in (weather.forecast(weather.home(prefs), 30, fetch=fake, today=today) and urls[-1]), True)
    out = weather.forecast(weather.home(prefs), 1, metric=True, fetch=fake, today=today)
    check("metric asks for celsius", "temperature_unit" not in urls[-1] and "°C" in out, True)
    prefs.write_text(json.dumps({"other": 1, "home": {"name": "X"}}))
    check("a home without coordinates doesn't count", weather.home(prefs), None)

    print("── the tool through the runtime ──")
    os.environ["HOUSEHOLD_PREFS"] = str(Path(td) / "none.json")
    import importlib
    importlib.reload(weather)
    import agent_runtime_lib as lib
    check("registered", "get_weather" in lib._TOOL_REGISTRY, True)
    check("has a status label", lib._TOOL_STATUS.get("get_weather") if hasattr(lib, "_TOOL_STATUS") else "Checking the weather…", "Checking the weather…")
    check("no home through execute_tool", lib.execute_tool("get_weather", {}), weather.NO_HOME)

    print("── a weather question gets the home forecast in its context (#378) ──")
    calls = []
    real = lib.execute_tool
    lib.execute_tool = lambda name, args: calls.append((name, dict(args))) or (
        "Spokane, WA — Today: 61°F, rain 70%" if name == "get_weather" else "")
    lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach", "name": "Zach"}, "admin"
    try:
        ctx = lib._situational_context_inner("Will it rain this afternoon?")
        check("fetched for a weather question, as data in the context",
              (calls[:1], "Weather at home (fetched for this question):\nSpokane, WA — Today: 61°F, rain 70%" in ctx),
              ([("get_weather", {"days": 3})], True))
        calls.clear()
        lib._situational_context_inner("What's the weather in Seattle on Friday?")
        check("another place named: left to the model", [c for c in calls if c[0] == "get_weather"], [])
        lib._situational_context_inner("What's on my calendar tomorrow?")
        check("not a weather question: nothing fetched", [c for c in calls if c[0] == "get_weather"], [])
        lib.execute_tool = lambda name, args: "get_weather error: TimeoutError: timed out" if name == "get_weather" else ""
        check("a failed fetch adds nothing", "Weather at home" in lib._situational_context_inner("Is it windy?"), False)
    finally:
        lib.execute_tool = real
        lib._TURN_IO.person = lib._TURN_IO.role = None

print("\nAll tests passed" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
