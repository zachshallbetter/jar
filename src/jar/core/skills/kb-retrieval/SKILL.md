---
name: kb-retrieval
description: Retrieves facts, preferences, and knowledge from the user's personal knowledge base. Use when answering questions about the user's household, preferences, past decisions, stored notes, or any fact that may have been saved previously. Always check the KB before guessing.
triggers: remember that, do you remember, what did we decide, we decided, decided about, my notes, notes about, write down, wrote down, license plate, what's our, what is our, titan's, text me, texted me, where did i put, look up what, find my notes
---

## Overview
Search and retrieve from the user's structured knowledge base (Titan KB) using `kb_retrieve` for semantic search or `kb_list` for browsing by category.

## When to Use
- User asks about something they may have told the agent before
- Question involves preferences, household details, routines, or personal context
- Before answering a factual question that could be in the KB, run `kb_retrieve` first
- Explicitly asked to "remember", "recall", or "look up" something

## Core Process
1. Call `kb_retrieve` with a concise keyword query
2. If results are found, ground your answer in them — cite the source
3. If nothing is stored, say so ("that isn't stored") and ask the user; never answer a household fact from general knowledge
4. If the user gives you something worth keeping, save it with `ingest_text` (private to them unless they say it's the household's)

## Rules
- Never fabricate KB results — only surface what `kb_retrieve` actually returns
- If the knowledge base is offline, say so; don't fill the gap with a guess
- `kb_list` is for browsing, not answering — use `kb_retrieve` for Q&A
