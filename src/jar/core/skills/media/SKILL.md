---
name: media
description: What is on the TVs and speakers and controlling them (play, pause, volume, mute, opening an app such as Apple Music or Disney+ on the Apple TV), and music on the iPhone. Use for any question about what is playing, what someone is watching, the TVs, speakers or music.
triggers: playing, what's playing, plex, tv, movie, watching, song, music, album, play, pause, podcast, volume, mute, apple tv, netflix, disney, turn on the tv, the living room tv
---

## Overview
The TVs and speakers are in Home Assistant on titan: the "TVs and speakers" line in your context, or
`home_status` with kind media_player, from the Mac or the iPhone. Change one with `home_control`:
play, pause, volume, mute; on an Apple TV also open an app by name and press remote buttons. Music on
the iPhone itself is `play_music`, `music_control` and `now_playing`, from the iPhone only.

## When to Use
- "What's on the living room TV?" / "Is anything playing?" / "What are we watching?"
- "Pause the TV" / "Turn it up" / "Mute the living room"
- "Open Disney+ on the Apple TV" / "Put Apple Music on the TV"
- "Play Kind of Blue" / "What's this song?" (on the iPhone)

## Core Process
1. Read what's playing from the TVs line or `home_status`. That is the answer: report the title, the
   TV and how far along when it says.
2. Change one device with one `home_control` call, then say what changed in the tool's words.
3. Something the TV can't be told from here (rewind 15 minutes, play a particular artist on the TV):
   say so in one line and offer what works (open Apple Music on the TV, or play it on the iPhone).
   Never reach for Shortcuts, AppleScript or the Mac's shell to control a TV.

## Rules
- Say only what the tool reported. Resuming a player is not "playing The White Stripes" (09-30).
- There is no Plex server. `get_plex_state` is only for a question about Plex itself; if it doesn't
  answer, say so and don't call it again.
- From the Mac, the iPhone's Music app can't be reached: say to ask from the iPhone.
