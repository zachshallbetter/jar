# Project Intent

**Project ID:** `jar`
**Project Name:** `Jar`
**Status:** Active (Stage 1 implementation initiated 2026-10-29)
**Bootstrap:** Formal Project Bootstrap 0.6.0 templates

## Originating intent

Modern voice and home assistants (Amazon Echo, Google Nest, Apple Siri) operate under a cloud-first surveillance architecture: raw audio and household interactions leave the home, intelligence is rented via recurring subscription models or monetized via ad networks, and devices become inert bricks during network outages.

**Jar** changes this condition: a turnkey, sovereign household AI appliance. It brings the full capabilities of Titan Node—local LLM inference, world models, deterministic privacy fences, and multi-device coordination—into a self-contained, single-process, plug-and-play architecture that can be mass-produced as dedicated hardware or installed on any existing personal computer.

- **What condition should change.** Household intelligence runs 100% locally on owned silicon; family members maintain strict, cryptographically enforced privacy boundaries; the home functions seamlessly without active internet connections.
- **For whom.** Real households—parents, children, and guests across multiple personal devices (macOS, iOS, watchOS, tvOS, and Android via the Jellybean client).
- **Why the project exists.** Privacy and home autonomy cannot exist when the computational core of the home is rented from third-party cloud vendors.
- **What counts.** A zero-config installation that provisions an appliance node, serves the out-of-box setup wizard (`/setup`), and pairs securely with Jellybean in under five minutes.
- **What does not count.** A cloud-dependent wrapper, a manual server fleet requiring ongoing DevOps maintenance, or a single-user desktop chatbot.

## Desired state

```text
CURRENT CONDITION
  Home assistants send personal audio and schedules to corporate clouds;
  Local AI setups require multi-GPU server racks and complex fleet operations.
      ↓
FORMAL / OPERATIONAL WORK
  Single-process appliance architecture, SQLite-backed state, hardware-adaptive model
  profiles, out-of-box web onboarding, and zero-trust device pairing.
      ↓
QUALIFIED TARGET CONDITION
  A standalone, tamper-resistant household appliance ("Jar") that anyone can plug
  into their router to instantly power private, sovereign local AI for their family.
```

## Constraints

```text
ownership / licensing   Proprietary / MIT evaluation; commits authored by the owner.
privacy / data          Local-first, sovereign by default; zero external telemetry;
                        read-time privacy fences prevent cross-member data leakage.
security                Zero open inbound ports; local setup key required for first-run
                        onboarding; hardware TPM / LUKS2 encryption on dedicated hardware.
platform                Linux (Ubuntu/Debian, x86_64 and aarch64) and macOS (Apple Silicon & Intel).
dependencies            Standard Python 3.10+, SQLite3, FastAPI, Uvicorn; zero external fleet daemons.
human authority         Parents and adults maintain explicit authority; kids cannot alter household
                        fences; irreversible actions require confirmation.
```

## Non-goals

Building a proprietary cloud hosting platform. Re-implementing a distributed multi-node Kubernetes scheduler. Requiring proprietary GPUs when standard CPUs or Apple Silicon are present.
