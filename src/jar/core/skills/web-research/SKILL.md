---
name: web-research
description: Searches the web and fetches URLs to answer questions requiring current information. Use when the user asks about news, prices, schedules, documentation, how-to guides, product comparisons, or anything that may have changed after the model's knowledge cutoff.
triggers: weather, rain, forecast, latest, current, docs, documentation, official, link to, news, price, prices, release, population, search the web, online, is it safe
---

## Overview
Use `web_search` for current information and `fetch_url` to read specific pages in full. Combine both for thorough research.

## When to Use
- Questions about current events, prices, availability, or recent changes
- Documentation lookup for libraries, APIs, or tools
- "What is / how do I / find me information about X"
- Confirming facts that the model might have stale information on

## Core Process
1. Formulate a precise search query — avoid vague or over-broad terms
2. Run `web_search`; scan results for the most authoritative sources
3. Use `fetch_url` on the top 1-2 sources to read full content
4. Synthesize a clear answer; cite sources with URLs
5. If the first search misses, refine the query and try again

## Rules
- Don't present search snippet text as the full answer — fetch and read the source
- Always cite sources in the final answer
- For medical, legal, or financial questions, note these are informational only
- Prefer official documentation, primary sources, and recent dates over blog posts
