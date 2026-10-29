---
name: home-automation
description: Controls smart home devices, checks device states, and runs household automations via Titan runtimes. Use when the user asks to turn on/off lights, lock doors, check cameras, adjust thermostat, run Shortcuts, or get the current state of any home device.
triggers: lights, light, lamp, thermostat, temperature inside, inside the house, turn up the heat, turn down the heat, air conditioning, lock, locked, unlock, door, doors, garage, camera, cameras, turn on, turn off, switch on, switch off, home assistant, fan, blinds
---

## Overview
The house's devices are in Home Assistant on titan. Read them with `home_status` (lights, switches,
the thermostat's mode, setpoint or range, temperature and humidity, TVs and speakers, locks, doors,
cameras); change one with `home_control`. The Mac's screen tools have nothing to do with the house.

## When to Use
- "Turn on/off the lights", "dim the kitchen to 40%"
- "What's the thermostat set to?", "Is it warm in here?", "Is the front door locked?"
- "Set it to 72", "turn the heat on", "pause the living room TV"
- Any request about a physical device in the house

## Core Process
1. Unsure of the device or its state: `home_status` first (filter by name or kind).
2. Change it with `home_control` (device as Home Assistant names it, action, value).
3. Say what changed in plain words ("The hallway is now 70–74°"). Honeywell's cloud can take a few
   minutes to show a thermostat change; say so if asked right after.

## Thermostat
- On heat or cool, one number is the setpoint.
- On Auto (heat_cool) it holds a range. One number ("set it to 72") moves the range to centre on it
  and keeps the mode; `home_control` does that and says the new range. Tell the user the range.
- Never change the mode (action set_mode) unless the user asks for a mode ("switch to heat", "turn the
  AC on", "turn it off"). A temperature alone is not a request to change the mode.
- A range they name ("68 to 72") is set as given (at least 2° apart).

## Rules
- Never claim a device changed without a `home_control` result that says Done.
- Locks, alarms, garage doors, cameras and Home Assistant scripts are hands-off: titan only reads
  them. Tell the user to use the Home Assistant app for those.
- Several devices match: ask which, listing them.
- Home Assistant unreachable: say so; don't guess the state.
