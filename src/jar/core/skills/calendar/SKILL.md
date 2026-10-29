---
name: calendar
description: Manages calendar events and reminders via EventKit on the user's Mac or iPhone. Use when the user asks to schedule, reschedule, cancel, or list events, set reminders or alarms, check their schedule for a time period, or find conflicts in their calendar.
triggers: calendar, schedule, reschedule, appointment, appointments, dentist, doctor, meeting, event, events, reminder, reminders, remind me to, remind me in, remind me about, remind me at, remind me on, remind me later, alarm, wake me, am i free, are we free, am i busy, are we busy, busy on, busy at, double-booked, double booked, free on {day}, free {day}, what's on {day}, whats on {day}, what is on {day}, anything on {day}, anything {day}, do i have * {day}, do we have * {day}, set a reminder, cancel, on my plate, plan around, leave for, pickup, pick up, book a, check that off, how many things, lesson {day}, lesson * {day}, what time is * {day}, push * back, move my, cancel the, on my calendar
---

## Overview
Read and change calendar events and reminders with `list_calendar_events`, `create_calendar_event`, `update_calendar_event`, `delete_calendar_event`, `list_reminders`, `create_reminder`, `complete_reminder`, `update_reminder`, `delete_reminder` and `set_alarm`. The household shares calendars: a calendar can be one person's, or shared by several people who each have their own part of it.

## When to Use
- "Schedule a meeting / add an appointment / block time"
- "What's on my calendar today / this week / tomorrow?"
- "Remind me to / set an alarm for"
- "Cancel / move / reschedule [event]"
- Proactive conflict detection when the user mentions a new commitment

## Core Process
1. For queries: call `list_calendar_events` with appropriate date range, summarize clearly
2. For new events: take the title, day, time and place from the user's message and create it; the Mac or iPhone shows the user what it adds. If the time is missing, ask for the time, and only that (09-30: a 1:1 was booked at a time nobody gave).
3. For reminders: use `create_reminder` with a due date; for time-critical items use `set_alarm`
   - Reminders are not calendar events. "Which reminders…", "what's due…", "my to-dos": `list_reminders`, and answer only from what it returns; never list calendar events as reminders.
4. Never guess a time or a day; never ask to confirm what the user already said.
5. Repeating ("every Wednesday night", "every other week", "until June"): one `create_calendar_event` with `repeat` (`{"every": "week", "days": ["wed"]}`; `"interval": 2` for every other week; `until`/`count` when said). The result says "repeating …" when the series was made: if it doesn't, say plainly that you added one event and it doesn't repeat (an older app can't), never that it repeats (10-01: garbage night became one event and the reply didn't say so).
6. A household event ("that's for everyone") goes on the household's shared calendar: say so when creating, or move it with `update_calendar_event` `calendar`, never by deleting and adding it again.

## Whose event it is
Each listed event ends with its calendar: ` [calendar: Family, shared — about Leo]`.
- `— Robin's` (any household name): the event is on that person's calendar. It's theirs.
- `about Leo`: the title names Leo. His own event ("Leo's skate night") is his part of the calendar; something the user does for him ("Pickup Leo from school") is the user's. `about you` is the user's.
- `[at: Pearl Dental]` is where the event is (the place never says whose it is).
- A shared calendar with nothing marked is the household's (say its name: "Family: Movie night").
- Give the agenda by person when more than one is involved: the user's events first, then "Robin: …", "Leo: …". Keep other people's events in; they matter for coordinating (who drives, who's home).
- "Am I free?" counts only the user's events. Answer Yes or No first ("Yes, you're free at 11"), then what's around it. Mention anyone else's that affect them ("Leo's skate night is at 6 — is someone driving him?").

## Getting there (iPhone)
- "When should I leave for the dentist?", "how long to get to Pearl Dental?", "am I going to be late?":
  `travel_time` with `destination` as they said it. An event's title works ("the dentist"); its start
  becomes the arrival time unless they give one. `mode` transit or walk only when they say so.
- Give the answer in their terms: when to leave, how long it takes, and how late if it says so.
  It's from "this iPhone": never mention coordinates or where they are now.
- "Which one: …": ask which they mean, naming the choices. Then call again with the same
  `destination` and that event's `on` (from its "on=…").
- "… has no place on it": ask where it is (and offer to add the place with update_calendar_event
  `location`). "… is a call, not a place": no trip; say so.
- No `travel_time` in your tools (the Mac): don't say you'll check. Answer now from the event: its start
  time and a time to leave with room to spare (about 30 minutes before for somewhere nearby, "leave by 8:30"),
  and say their iPhone can work out the exact drive.

## Changing and cancelling
- Move or rename with `update_calendar_event`; cancel with `delete_calendar_event` when the user asks. Find the event with `list_calendar_events` first and pass its title and day (`on`).
- Several at once ("clear tomorrow"): list exactly which ones, ask the user to confirm that list, and delete only after they say yes.
- A repeating event: only that occurrence, unless the user says the whole series.
- A shared calendar changes for everyone on it. Before changing or cancelling there, say who else will see the change ("It's on Family, shared with Robin"). Ask before touching an event that's someone else's (`— Robin's`, or their own event on a shared calendar), even a single one.
- After a delete, say what was removed and that "undo" restores it.

## Rules
- Use ISO 8601 format for all date/time params (e.g. 2026-07-08T16:00)
- Timezone is America/Los_Angeles unless the user specifies otherwise
- If EventKit authorization is denied, explain and suggest Settings > Privacy > Calendars
