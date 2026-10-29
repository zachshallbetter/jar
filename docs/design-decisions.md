# Jar — Design Decisions

This document records the reasoning behind Jar's architecture: what was chosen, what was rejected, and why. It is a permanent engineering log, not a marketing brief. Rejected paths are preserved deliberately to prevent re-proposing abandoned directions.

---

## 1. Origin Story

Jar began as household-box work inside `titan-node` (issue #414, `scripts/box_*.py`, `docs/box-*.md`). The core idea: a single-box AI appliance that replaces the 42-port, 19-service fleet of Titan with something a non-engineer can install, own, and trust.

The immediate trigger was sharing: the developer wanted to give a sibling a way to run the same household AI stack on his own PC via a simple installation — not a Tailscale tunnel into the developer's machine, not a cloud subscription, but a real local node he owns.

The mass-production thought experiment ("what if we were selling these?") forced the architecture to be crisp: no Titan-specific paths, no fleet coordinator assumptions, no Postgres.

---

## 2. Architectural Decisions

### 2.1 Single Process, SQLite-Only

**Decision:** Jar runs as one Python process. All persistence is SQLite3 under `$JAR_HOME`.

**Rejected:** Postgres, Redis, Rust service mesh (Titan uses 19 Rust services + Postgres).

**Rationale:** A household appliance that requires a DBA to maintain is not a household appliance. SQLite needs zero configuration, zero background daemons, zero port conflicts, and can be backed up with `cp`. Titan's multi-service architecture exists because of fleet scale; a single box has no fleet.

**Risk accepted:** SQLite write throughput caps out around 1,000 writes/sec under WAL mode. For a household node serving 1–6 people, this is not a constraint.

---

### 2.2 One Port

**Decision:** Jar exposes a single HTTP port (default 7779, configurable via `$AGENT_API_PORT`).

**Rejected:** Titan's 42-port topology (API, coordinator, knowledge base, graph sync, mail, calendar, push, canary, …).

**Rationale:** Every open port is an attack surface and a support ticket. A single port behind a local reverse proxy or Tailscale is enough. Internal subsystems communicate in-process via function calls, not network sockets.

---

### 2.3 `$JAR_HOME` — No Hardcoded Paths

**Decision:** Every file Jar writes — databases, uploads, generated content, logs, models — lives under `$JAR_HOME` (default `~/.jar`). Zero `/ai/` or `/tmp/` assumptions.

**Rejected:** Hardcoded `/ai/datasets/`, `/ai/tmp/`, `/ai/projects/` paths inherited from Titan.

**Rationale:** Titan's host has a purpose-built RAID with `/ai/` as the AI data root. A sibling's PC does not. An appliance sold commercially cannot assume any filesystem layout. This constraint was enforced at the founding commit: any code touching `/ai/` outside `src/jar/core/` is a bug.

---

### 2.4 Hardware-Adaptive Model Selection

**Decision:** `jar profile` detects hardware at runtime and recommends the best model that fits in available VRAM/RAM without manual configuration.

**Rationale:** The developer's Titan host has 3× GTX TITAN X (Maxwell, sm_52, 12 GB each, 36 GB total VRAM). A sibling's PC might be a 2021 MacBook Pro with 16 GB unified memory or a Windows desktop with a single RTX 3060. Jar must work across all of them without the user knowing what `Q4_K_M` means.

**Hardware profiles implemented in `src/jar/hardware.py`:**

| VRAM / RAM | Recommended model |
|---|---|
| ≥ 64 GB unified | Qwen3.6-35B-A3B (full MoE) |
| ≥ 24 GB VRAM | Qwen3-30B-A3B-Q4_K_M |
| ≥ 12 GB VRAM | Qwen3-8B-Q4_K_M |
| ≥ 8 GB VRAM | Qwen3-8B-Q4_K_M (tight) |
| CPU-only | Qwen3-1.7B-Q4_K_M |

---

### 2.5 Privacy Fences Are a First-Class Invariant

**Decision:** Kid/guest data is always fenced from adult/owner data at the data layer, not the application layer. No application-level bypass can cross a fence.

**Rationale:** This was established in Titan's world model (`world_fences.py`, `docs/world-model-fences.md`) through hard experience. A household AI that can be instructed to read a child's messages is a liability. Jar inherits these fences from `src/jar/core/world_fences.py` and `src/jar/core/privacy_eval.py`.

**Invariant:** Any Jar release that weakens privacy fences is a regression, not a feature.

---

### 2.6 Setup Wizard — OOBE in 3 Minutes

**Decision:** First boot presents an interactive setup wizard at `http://localhost:7779/setup` (or the LAN IP). No terminal required.

**Rejected:** CLI-only setup, environment variable files that users must hand-edit.

**Rationale:** "Share with a sibling" requires zero-terminal installation. The wizard collects: household name, owner account, local model preference, optional cloud API key. Everything else defaults safely.

The setup wizard code (`box_setup.py`, `box_setup_page.html`) already existed in Titan's codebase and is carried into `src/jar/core/` unchanged.

---

### 2.7 Cloud Hybrid Is Optional, Not Required

**Decision:** Jar runs fully offline by default. Cloud (Gemini API) is opt-in during setup.

**Rejected:** Making cloud API key mandatory for first boot.

**Rationale:** Privacy-first households may refuse all cloud routing. The appliance must work without internet. Cloud hybrid improves quality on long-context tasks and multimodal input; it is presented as an upgrade, not a prerequisite.

---

## 3. Rejected Paths (Permanent Record)

### 3.1 GPU Upgrade on Titan

**Considered:** Install newer GPUs in the Titan host (Maxwell-era X99 board) to get Tensor Cores.

**Rejected:** The X99 board's PCIe topology fragments bandwidth across all three GPUs. The board does not support PCIe 4.0 or NVLink. Upgrading one Maxwell card to an Ampere card would not restore full bandwidth. A full board + GPU replacement approaches the cost of purpose-built inference silicon.

**Alternative chosen:** Target new-build hardware (AMD Strix Halo / Ryzen AI Max+ 395, 64 GB unified at 212 GB/s) for next-generation Jar hardware.

---

### 3.2 Recompiling CUDA for sm_52

**Considered:** Rebuild llama.cpp or PyTorch CUDA kernels from source to optimize for Maxwell (sm_52).

**Rejected:** CUDA is NVIDIA proprietary. Source is not fully available. `mma.sync` (Tensor Core matrix multiply) is a hardware instruction that does not exist on Maxwell silicon — no software path can add it. Recompiling changes code generation but cannot synthesize missing hardware units.

**Conclusion:** Maxwell's FP16 throughput is ~1/4 of a current Ampere card at the same TDP. Software optimization paths exist but are bounded:

| Path | Est. gain | State |
|---|---|---|
| Speculative decoding (draft model) | 2–3× | Implementable |
| Prompt lookup decoding | 1.5–2× on repetitive text | Implementable |
| KV cache quantization (q8_0) | Fits larger model in 12 GB | Implemented in llama.cpp |
| GPU isolation (one model per card) | Eliminates PCIe split overhead | Requires load balancer |
| Asymmetric nightly offloading | Frees VRAM for overnight batches | Speculative |

---

### 3.3 Docker Distribution

**Considered:** Ship Jar as a Docker image.

**Rejected (for now):** Docker adds 1–2 GB overhead, complicates GPU passthrough (NVIDIA container toolkit required, varies by distro), and requires Docker Desktop on Windows/macOS which itself requires a subscription for commercial use. For a household appliance, the `install.sh` → Python venv path is simpler and works on all three platforms without additional licensing.

**Not permanently closed:** A Docker image may be appropriate for cloud-hosted Jar instances or CI environments. It is a future distribution channel, not the primary one.

---

### 3.4 Rust Runtime

**Considered:** Port Jar's server to Rust (following Titan's service pattern).

**Rejected:** The household-appliance performance target (1–6 concurrent users, ~10 req/s peak) is well within Python's async capacity. The setup wizard, privacy eval, and world model are all Python. A Rust rewrite buys nothing except build complexity and a larger contributor barrier.

---

## 4. Distribution Strategy

Three sharing modes, in order of friction:

| Mode | Setup | Who owns the data |
|---|---|---|
| Virtual Jar (Tailscale into Titan) | Zero install; share Tailscale invite | Developer (data stays on Titan) |
| Jar on sibling's PC (`install.sh`) | One `curl \| bash` or `git clone && ./install.sh` | Sibling (data on his machine) |
| Physical Jar box | Ship the box | Sibling (data on his hardware) |

The developer's immediate goal (sharing with a sibling) is satisfied by mode 2. Mode 3 is the long-term commercial path.

---

## 5. Mass Production Vision

If Jar were a commercial product:

- **Chassis:** Anodized aluminum, single LED ring (status indicator only — not decorative)
- **Boot:** Immutable OS root (A/B partitions), TPM-sealed disk encryption
- **First-run:** OOBE wizard completes in under 3 minutes, no terminal required
- **Update:** Atomic OTA updates to inactive partition; rollback on failure
- **Privacy architecture:** All inference local by default; cloud routing requires explicit per-request consent
- **Target hardware (2025 BOM):** AMD Ryzen AI Max+ 395 (Strix Halo), 64 GB unified LPDDR5X at 212 GB/s, 2 TB NVMe — runs Qwen3.6-35B at ~77 tok/s decode
- **Price point:** Under \$1,500 at volume (APU + memory + NVMe + chassis + assembly)

None of this requires new software beyond what Jar already has. The hardware profile recommendation in `src/jar/hardware.py` already detects and targets Strix Halo.

---

## 6. Relationship to Titan Node

`titan-node` is a developer rig and fleet coordinator. Jar is an appliance node. They share:

- World model (`world.py`, fences, claims)
- Privacy eval pipeline
- Setup wizard (`box_setup.py`)
- Knowledge base runtime

They do not share:

- Rust service mesh (Titan only)
- Postgres (Titan only)  
- Coordinator JWTs and fleet topology (Titan only)
- `/ai/` filesystem assumptions (Titan only)

`src/jar/core/` is a snapshot of `titan-node/scripts/` at the Jar founding commit. As the two diverge, the snapshot will need periodic reconciliation — or the shared runtime should be extracted into its own package.

---

*This document is append-only. Do not delete entries under §3 (Rejected Paths) regardless of future direction changes.*
