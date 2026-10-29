# Jar 🏺

**Jar is a sovereign, self-contained household AI appliance node.**

Built as a dedicated appliance distribution of the Titan agent stack, Jar transforms any modern computer or mini-PC into a private, always-on household brain that powers [Jellybean](https://github.com/zachshallbetter/jellybean) across Macs, iPhones, Apple Watches, and smart TVs.

---

## Why Jar Exists

Most home assistants (Alexa, Google Nest, Siri) send voice recordings, private calendars, and household queries into the cloud.

Jar is built on the opposite philosophy:
* **100% On-Premise Execution:** Local models run on local silicon. If your home internet goes down, your house keeps working.
* **Granular Privacy Fences:** A unified world model that understands the family (parents, kids, guests), but strictly fences sensitive adult/financial records so they never leak to children or shared screens.
* **Single-Process Simplicity:** Eliminates the need for Kubernetes or multi-service micro fleets. All state is stored in standard, portable SQLite files in one folder (`~/.jar/data`).
* **Zero-Config Onboarding:** Turn on the machine, scan the local setup QR code on your phone, name your home, and start interacting.

---

## Installation

### 1. Requirements
* macOS (Apple Silicon or Intel, macOS 12+) or Linux (Ubuntu, Debian, Fedora, Arch; x86_64 or aarch64)
* Python 3.10+

### 2. Install in One Step
```bash
git clone https://github.com/zachshallbetter/jar.git
cd jar
./install.sh
```

The installer will:
1. Profile your hardware (RAM, CPU, and GPU accelerators like Apple Silicon Metal or NVIDIA CUDA).
2. Configure a local virtual environment with all required dependencies.
3. Set up the local SQLite database roots in `~/.jar/`.
4. Register a background daemon (`systemd` on Linux, `launchd` on macOS).
5. Print your unique setup URL.

---

## First-Run Onboarding (`/setup`)

Open the setup URL printed by the installer in your browser:

```
👉  Setup URL: http://<your-machine-ip>:7770/setup#k=<your-setup-key>
```

The web setup wizard walks you through:
1. **Naming your home** (e.g. "The Family House")
2. **Naming the owner** and claiming admin rights
3. **Adding family members** (adults, kids, guests) with isolated bubbles
4. **Pairing devices** with Jellybean via QR codes
5. **Connecting smart home devices** (Home Assistant & Matter)

Once onboarding is completed, `/setup` closes permanently.

---

## Management CLI

Jar includes a management CLI located at `bin/jar` (and linked to `~/.local/bin/jar`):

```bash
jar run       # Run the server in the foreground
jar start     # Start the daemon in the background
jar stop      # Stop the background daemon
jar status    # Check service health and PID
jar url       # View the onboarding URL (if setup is still open)
jar profile   # Inspect local hardware and recommended model fit
jar logs      # Stream live operational logs
```

---

## Documentation

* [Architecture & Storage Contract](docs/architecture.md)
* [Hierarchy of Authority (AUTHORITY.md)](AUTHORITY.md)
* [Project Intent (PROJECT_INTENT.md)](PROJECT_INTENT.md)
