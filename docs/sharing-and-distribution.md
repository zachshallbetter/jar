# Sharing & Distribution

This document covers how to give someone else access to the same household AI stack — from a zero-install shared session to a full standalone installation on their own hardware.

---

## Three Modes

### Mode 1 — Virtual Jar (Tailscale into an existing Jar/Titan node)

The recipient gets access to your running instance over Tailscale. Nothing installs on their machine.

**Setup:**
1. Install Tailscale on both machines.
2. Add the recipient's machine to your Tailscale network (or share the node via Tailscale's share feature).
3. The recipient opens `http://<your-tailscale-ip>:7779` in a browser.

**Trade-offs:**

| | |
|---|---|
| Setup time | ~5 minutes |
| Their data ownership | None — all data stays on your machine |
| Works offline on their end | No — requires Tailscale tunnel |
| Privacy | Your Jar instance; you can see their conversations |
| Best for | Temporary access, demos, "try before you install" |

---

### Mode 2 — Jar on Their PC (`install.sh`)

The recipient installs Jar on their own machine. Their data stays on their hardware. This is the primary sharing path.

**One-liner (from a release):**
```bash
curl -fsSL https://github.com/zachshallbetter/jar/releases/latest/download/install.sh | bash
```

**Or from source:**
```bash
git clone https://github.com/zachshallbetter/jar.git
cd jar
./install.sh
```

After install, `jar run` starts the local node. First-run wizard at `http://localhost:7779/setup`.

**Trade-offs:**

| | |
|---|---|
| Setup time | 5–15 minutes (model download is the long step) |
| Their data ownership | Full — everything on their machine |
| Works offline | Yes, after initial model download |
| Privacy | Complete — separate instance, separate data |
| Best for | Long-term independent use |

**What the install does:**
1. Detects hardware and prints a profile (GPU, VRAM, recommended model).
2. Creates a Python venv at `~/.jar/venv`.
3. Installs Jar and dependencies.
4. Links `jar` CLI to `~/.local/bin/jar`.
5. Registers a system service (macOS LaunchAgent or systemd unit on Linux).
6. Prints the setup URL.

No admin rights required on macOS or Linux (user-scope install). On Windows, WSL2 is the supported path.

---

### Mode 3 — Physical Jar Box

Ship a physical box with Jar pre-installed. The recipient plugs it in; first-boot wizard runs automatically.

**Target hardware (2025 BOM):**
- AMD Ryzen AI Max+ 395 (Strix Halo APU)
- 64 GB LPDDR5X unified memory @ 212 GB/s
- 2 TB NVMe
- Compact chassis, single status LED ring

**What's pre-installed:**
- Linux (immutable root, A/B partitions)
- Jar daemon (autostart on boot)
- Qwen3.6-35B-A3B-Q4_K_M (model pre-loaded to NVMe)

**First-run experience:**
1. Plug in power and ethernet.
2. Open a browser on any device on the same network.
3. Navigate to `http://jar.local` (mDNS) or the IP shown on the box display.
4. Complete the 3-minute OOBE wizard.

**Trade-offs:**

| | |
|---|---|
| Setup time | ~3 minutes (model pre-loaded) |
| Their data ownership | Full — their box, their data |
| Works offline | Yes, fully air-gapped capable |
| Privacy | Complete — no cloud dependency by default |
| Best for | Non-technical users, gifting, commercial distribution |

---

## Practical Recommendation for Sibling Sharing

For giving a sibling access to run Jar on his own PC:

1. **Start with Mode 1** (Tailscale) so he can try it immediately with zero install.
2. **Move to Mode 2** once he decides he wants his own instance. The `install.sh` handles everything.
3. **Mode 3** is the upgrade path if he wants a dedicated box.

The install script auto-detects his hardware and recommends the right model. If he has a mid-range gaming GPU (RTX 3060 or better), he gets a very usable experience at 25–40 tok/s. If he's CPU-only, he gets a slower but functional instance.

---

## Privacy Across Modes

In Mode 1 (shared instance), all household members share the same world model and knowledge base. Privacy fences still apply (kids vs. adults, guests vs. household), but the operator (you) has access to system logs.

In Modes 2 and 3, each instance is sovereign. No data crosses between instances unless explicitly exported and imported. There is no fleet coordination, no shared database, no sync.

---

## Release Artifacts

A Jar release (`./install.sh` at the root) produces:

| Artifact | Purpose |
|---|---|
| `install.sh` | Primary installer — works on macOS, Linux, WSL2 |
| `dist/jar-<version>-py3-none-any.whl` | pip-installable wheel for custom deployments |
| `dist/jar-<version>.tar.gz` | Source distribution |

The GitHub release page (when the remote repo is created) will host these alongside checksums. The one-liner `curl | bash` install fetches the latest `install.sh` from the release assets.
