# Security Policy

## Reporting a vulnerability

If you find a security issue in Jar, please report it privately rather than opening a
public issue. Use GitHub private vulnerability reporting:

<https://github.com/zachshallbetter/jar/security/advisories/new>

Include:

- a description of the issue and its impact,
- steps to reproduce (a minimal example is ideal),
- the affected component and version or commit.

This is a solo-maintained project. Handling is best-effort with no SLA: there is no
guaranteed response or fix time. Please give a reasonable window to ship a fix before any
public disclosure.

## Scope

Jar is a single-process household AI appliance. It serves HTTP on one port (default 7770;
the host defaults to all interfaces and is set by `AGENT_API_HOST`) and keeps its state in
SQLite files under `~/.jar`. The relevant surface is the setup wizard and its one-time key,
device pairing, API authorization, the privacy fences that filter reads between household
members, and handling of data under `~/.jar`. Reports about any of these are in scope.

Jar can call out to optional cloud model tiers; report a leak of household data to any
external service.

## Supported versions

Jar is at version 0.1.0 and has no tagged releases. Only the latest commit on `main`
receives fixes.
