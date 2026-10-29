#!/usr/bin/env python3
"""Weather for the household: the get_weather tool.

Open-Meteo (no key; https://open-meteo.com): a place name goes to its geocoder, coordinates to its
forecast API. Nothing else about the household leaves titan. The home place is stored once, when
the user says where home is (get_weather with remember=true), in HOUSEHOLD_PREFS (default
/ai/datasets/connectors/household.json), so "what's the weather tomorrow?" needs no question after
that. Before then the tool says so, and the model asks the user for their town instead of
searching their Mac for it.

Units: Fahrenheit and mph unless AGENT_WEATHER_UNITS=metric.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import local_time

PREFS = Path(os.environ.get("HOUSEHOLD_PREFS", "/ai/datasets/connectors/household.json"))
# Without a saved home, Home Assistant's own home location (set at its onboarding) is used.
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST = "https://api.open-meteo.com/v1/forecast"
NO_HOME = ("The household's home town isn't set. Ask the user which town or city they're in (don't look "
           "for it on their Mac), then call get_weather with that place and remember=true.")

# WMO weather interpretation codes (Open-Meteo's weather_code).
_CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
          51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
          61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
          71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
          81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
          95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}

Fetch = Callable[[str], dict[str, Any]]


def _get(url: str) -> dict[str, Any]:
    import egress
    egress.note("weather")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "titan-household"}), timeout=8) as r:
        return json.loads(r.read().decode())


def home(prefs: Path | None = None) -> dict[str, Any] | None:
    try:
        h = json.loads(Path(prefs or PREFS).read_text()).get("home")
        return h if h and "latitude" in h else None
    except (OSError, ValueError):
        return None


def _save_home(place: dict[str, Any], prefs: Path | None = None) -> None:
    p = Path(prefs or PREFS)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        data = {}
    data["home"] = place
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def ha_home(http: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    """Home Assistant's own home location (set when it was onboarded), when titan has a token."""
    try:
        if http is None:
            import home_assistant as ha
            http = ha._http
        c = http("GET", "/api/config", None)
        if c.get("latitude") is None or c.get("longitude") is None:
            return None
        return {"name": "Home", "latitude": c["latitude"], "longitude": c["longitude"],
                "timezone": c.get("time_zone") or "auto"}
    except Exception:  # noqa: BLE001
        return None


def geocode(name: str, fetch: Fetch = _get, *, every: bool = False) -> Any:
    """'Spokane, WA' → the best match. Open-Meteo matches on the town name alone, so a trailing
    region ('WA', 'Washington', 'UK') is used to pick among same-named towns. every=True: all the
    matches, best first, each with its `score` (how well the trailing region matched) and Open-Meteo's
    `population` (None when it has none), for a caller that must notice a tie (#616); [] for none."""
    parts = [s.strip() for s in name.split(",") if s.strip()]
    if not parts:
        return [] if every else None
    q = urllib.parse.urlencode({"name": parts[0], "count": 10, "language": "en", "format": "json"})
    hits = fetch(f"{GEOCODE}?{q}").get("results") or []
    if not hits:
        return [] if every else None
    want = " ".join(parts[1:]).lower()

    def score(h: dict[str, Any]) -> int:
        if not want:
            return 0
        fields = [str(h.get(k) or "").lower() for k in ("admin1", "country", "country_code", "admin2")]
        return sum(1 for f in fields if f and (f == want or f.startswith(want) or want in f or
                                              (len(want) == 2 and _US_STATES.get(want.upper()) == f)))
    hits = sorted(hits, key=score, reverse=True)   # stable: Open-Meteo's own order within a score

    def place(h: dict[str, Any]) -> dict[str, Any]:
        label = ", ".join(x for x in (h.get("name"), h.get("admin1") if h.get("country_code") == "US" else h.get("country")) if x)
        pop = h.get("population")
        return {"name": label, "latitude": h["latitude"], "longitude": h["longitude"],
                "timezone": h.get("timezone") or "auto", "score": score(h),
                "population": int(pop) if isinstance(pop, (int, float)) and pop > 0 else None}
    if every:
        return [place(h) for h in hits]
    top = place(hits[0])
    return {k: top[k] for k in ("name", "latitude", "longitude", "timezone")}


def _forecast_today(current: dict[str, Any]) -> date | None:
    """The place's own date from Open-Meteo's `current.time` ("2026-09-30T22:15", local to the place)."""
    try:
        return datetime.strptime(str(current.get("time") or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None

def forecast(place: dict[str, Any], days: int = 3, *, metric: bool | None = None, fetch: Fetch = _get,
             today: date | None = None) -> str:
    metric = (os.environ.get("AGENT_WEATHER_UNITS", "").lower() == "metric") if metric is None else metric
    days = max(1, min(int(days or 3), 7))
    q = {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": place.get("timezone") or "auto",
         "forecast_days": days, "current": "temperature_2m,weather_code,wind_speed_10m",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"}
    if not metric:
        q.update(temperature_unit="fahrenheit", wind_speed_unit="mph")
    d = fetch(f"{FORECAST}?{urllib.parse.urlencode(q)}")
    deg, wind = ("°C", "km/h") if metric else ("°F", "mph")
    out = [place["name"]]
    cur = d.get("current") or {}
    if cur.get("temperature_2m") is not None:
        out.append(f"Now: {round(cur['temperature_2m'])}{deg}, {_CODES.get(cur.get('weather_code'), 'unknown')}"
                   + (f", wind {round(cur['wind_speed_10m'])} {wind}" if cur.get("wind_speed_10m") is not None else ""))
    dl = d.get("daily") or {}
    # The forecast's own "now" is in the place's zone; titan's host clock is UTC (10-01: every
    # Pacific evening "Today" was tomorrow's forecast).
    today = today or _forecast_today(cur) or local_time.today()
    for i, day in enumerate(dl.get("time") or []):
        dt = datetime.strptime(day, "%Y-%m-%d").date()
        name = "Today" if dt == today else "Tomorrow" if (dt - today).days == 1 else dt.strftime("%A")
        hi, lo = dl["temperature_2m_max"][i], dl["temperature_2m_min"][i]
        rain = (dl.get("precipitation_probability_max") or [None] * (i + 1))[i]
        wmax = (dl.get("wind_speed_10m_max") or [None] * (i + 1))[i]
        line = (f"{name} ({dt.strftime('%a %b')} {dt.day}): {_CODES.get(dl['weather_code'][i], 'unknown')}, "
                f"high {round(hi)}{deg}, low {round(lo)}{deg}")
        if rain is not None:
            line += f", {rain}% chance of rain"
        if wmax is not None and wmax >= (30 if metric else 20):
            line += f", wind up to {round(wmax)} {wind}"
        out.append(line)
    return "\n".join(out)


def today_summary(*, fetch: Fetch = _get, prefs: Path | None = None) -> dict[str, Any] | None:
    """Home's weather (titan-node#669), as facts: {place, now, sky, high, low, rain, unit} for the saved home, or None
    when no home is known or the forecast can't be read."""
    place = home(prefs) or ha_home()
    if not place:
        return None
    metric = os.environ.get("AGENT_WEATHER_UNITS", "").lower() == "metric"
    q = {"latitude": place["latitude"], "longitude": place["longitude"], "timezone": place.get("timezone") or "auto",
         "forecast_days": 1, "current": "temperature_2m,weather_code",
         "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"}
    if not metric:
        q["temperature_unit"] = "fahrenheit"
    try:
        d = fetch(f"{FORECAST}?{urllib.parse.urlencode(q)}")
    except Exception:  # noqa: BLE001 - Home shows no weather rather than an error
        return None
    cur, dl = d.get("current") or {}, d.get("daily") or {}
    if cur.get("temperature_2m") is None:
        return None
    first = lambda k: (dl.get(k) or [None])[0]   # noqa: E731
    return {"place": place.get("name"), "now": round(cur["temperature_2m"]), "sky": _CODES.get(cur.get("weather_code"), ""),
            "high": round(first("temperature_2m_max")) if first("temperature_2m_max") is not None else None,
            "low": round(first("temperature_2m_min")) if first("temperature_2m_min") is not None else None,
            "rain": first("precipitation_probability_max"), "unit": "C" if metric else "F"}


def get_weather(args: dict[str, Any], *, fetch: Fetch = _get, prefs: Path | None = None, today: date | None = None) -> str:
    """The tool. args: place (optional; default the saved home), days (1-7, default 3), remember (bool)."""
    place_name = str(args.get("place") or "").strip()
    remember = str(args.get("remember", "")).lower() in ("true", "1", "yes")
    if place_name:
        place = geocode(place_name, fetch)
        if not place:
            return f"Couldn't find a place called '{place_name}'. Ask the user for the town and state or country."
        if remember:
            _save_home(place, prefs)
    else:
        place = home(prefs) or ha_home()
        if not place:
            return NO_HOME
    text = forecast(place, args.get("days") or 3, fetch=fetch, today=today)
    return text + ("\n(Saved as home; later weather questions won't need the place.)" if place_name and remember else "")


_US_STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
              "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
              "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
              "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
              "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
              "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
              "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
              "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
              "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
              "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia"}
