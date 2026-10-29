# Jar Architecture

**Jar** is the single-process, turnkey household appliance edition of Titan.

```text
       ┌─────────────────────────────────────────────────────────────┐
       │                   JELLYBEAN APP CLIENTS                     │
       │        (Mac Menu Bar, iPhone, Apple Watch, Apple TV)        │
       └──────────────────────────────┬──────────────────────────────┘
                                      │ HTTP / SSE (:7770)
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │                         JAR NODE                            │
       │                                                             │
       │   ┌─────────────────────┐       ┌───────────────────────┐   │
       │   │   Setup Wizard      │       │     Agent Engine      │   │
       │   │  (/setup, claims)   │       │   (Turn loop, tools)  │   │
       │   └──────────┬──────────┘       └───────────┬───────────┘   │
       │              │                              │               │
       │   ┌──────────▼──────────┐       ┌───────────▼───────────┐   │
       │   │  Household Identity │       │      World Model      │   │
       │   │  (People & Devices) │       │   (Entities & Claims) │   │
       │   └──────────┬──────────┘       └───────────┬───────────┘   │
       │              │                              │               │
       │              └──────────────┬───────────────┘               │
       │                             ▼                               │
       │                 ┌───────────────────────┐                   │
       │                 │    Privacy Fences     │                   │
       │                 │  (Read-time filtering)│                   │
       │                 └───────────┬───────────┘                   │
       │                             ▼                               │
       │                 ┌───────────────────────┐                   │
       │                 │   SQLite State Root   │                   │
       │                 │     (~/.jar/data/)    │                   │
       │                 └───────────────────────┘                   │
       └─────────────────────────────────────────────────────────────┘
```

## Core Distinctions from Titan Node

| Feature | Titan Node (Developer Host) | Jar (Household Appliance) |
|---|---|---|
| **Form Factor** | Multi-GPU Ubuntu Server (3× GTX TITAN X) | Single-process binary or plug-in mini-PC (Strix Halo / Mac) |
| **Fleet Topology** | 19 Rust services + 3 brokers + Postgres/pgvector | Self-contained single daemon + SQLite3 |
| **Ports** | 42 listening internal ports | 1 listening external port (:7770) |
| **Setup Experience** | Manual SSH keys, systemd deployment scripts | Out-of-the-box web wizard (`/setup`) with one-time sticker key |
| **Storage Contract** | Distributed across `/ai/` mounts | Unified under `~/.jar/data/` (simple copy/paste backup) |
| **Egress / Privacy** | Configured per service with coordinator JWTs | Completely local SQLite + optional cloud fast tier |

## Storage Layout (`~/.jar`)

* `~/.jar/data/`: All persistent SQLite databases (`household.db`, `world.db`, `fences.db`, `tasks.db`, `todos.db`).
* `~/.jar/config/`: System identity keys, box setup markers, and appliance configurations.
* `~/.jar/logs/`: Local execution and turn audit logs.
* `~/.jar/tmp/`: Scratch storage for transient audio / document conversions.

