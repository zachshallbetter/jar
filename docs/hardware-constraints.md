# Hardware Constraints & Model Selection

This document covers why Jar targets specific hardware, what the Maxwell-era GPU analysis revealed, and how to select a model for any machine Jar runs on.

---

## 1. Why Unified Memory?

Jar's default hardware target is a **unified memory SoC** (Apple Silicon M-series or AMD Strix Halo). The reason is memory bandwidth:

- Large language models are memory-bandwidth-bound at inference time, not compute-bound.
- A discrete GPU with 12 GB GDDR6 has ~500 GB/s internal bandwidth but must cross PCIe (16 GB/s bidirectional) to reach system RAM.
- A unified memory SoC at 64 GB / 212 GB/s keeps the entire model weight and KV cache on one bus, eliminating the PCIe bottleneck and allowing models far larger than any single discrete GPU's VRAM.

**Concrete comparison (Qwen3-8B Q4_K_M at ~5 GB):**

| Platform | Bandwidth | Decode tok/s (est.) |
|---|---|---|
| GTX TITAN X (Maxwell, 12 GB) | 336 GB/s (GPU), 16 GB/s (PCIe) | ~18–25 |
| RTX 4090 (24 GB) | 1,008 GB/s | ~80–100 |
| Apple M4 Max (64 GB) | 546 GB/s | ~60–80 |
| Ryzen AI Max+ 395 (64 GB) | 212 GB/s | ~55–77 |

The Ryzen AI Max+ 395 (Strix Halo) is Jar's **primary Linux target** because it runs native Linux, has no macOS lock-in, and at volume costs under \$1,500 including chassis.

---

## 2. Maxwell GPU Analysis (GTX TITAN X, sm_52)

This section documents why the developer's existing Titan host (3× GTX TITAN X) is at the ceiling of software optimization.

### 2.1 What Maxwell Has

- **SM architecture:** sm_52 (Maxwell 2nd gen)
- **FP32 throughput:** 6.1 TFLOPS per card
- **FP16 throughput:** Same as FP32 (no native FP16 acceleration)
- **Memory:** 12 GB GDDR5 @ 336 GB/s per card
- **PCIe:** 3.0 x16 per card (theoretical 16 GB/s, actual ~12 GB/s bidirectional under load)

### 2.2 What Maxwell Lacks

- **Tensor Cores:** Introduced in Volta (sm_70, 2017). Maxwell has none. Tensor Cores accelerate `mma.sync` (matrix multiply-accumulate) — the dominant operation in transformer inference. Without them, every matmul runs as scalar FP32 on CUDA cores at ~1/4 the throughput of a modern Ampere card at the same TDP.
- **BF16 hardware support:** Maxwell supports FP16 storage but not accelerated BF16 compute. Most modern LLM kernels target BF16.
- **NVLink:** Maxwell consumer cards use PCIe only. Multi-GPU bandwidth is bounded by PCIe, not NVLink (which would be ~600 GB/s).

### 2.3 Why CUDA Cannot Be Rebuilt for Maxwell

CUDA is NVIDIA proprietary software. The GPU microarchitecture specification for sm_52 is not fully public. Critically:

- `mma.sync` is a **hardware instruction** on Volta+ silicon. It does not exist in Maxwell's instruction set.
- No amount of source-level recompilation can synthesize a hardware unit that is not present in the die.
- Recompiling llama.cpp or PyTorch from source with `-arch=sm_52` changes code generation for the subset of instructions Maxwell supports, but cannot add Tensor Core throughput.

**Conclusion:** Maxwell is a scalar FP32 GPU executing modern LLM kernels sub-optimally. The gap vs. Ampere is ~4× on throughput-per-watt for transformer workloads.

### 2.4 PCIe Fragmentation (3× Cards)

The Titan host's X99 board distributes PCIe lanes across three GPUs. Under typical firmware configurations:

- GPU 0: x16
- GPU 1: x8
- GPU 2: x8 (or x4 depending on slot)

When a model is split across multiple GPUs (tensor parallelism), every layer boundary requires a PCIe transfer. For a 7B model split 3 ways, this adds ~5–10 ms of transfer latency per token — approximately doubling wall-clock decode time vs. fitting the full model in one card's VRAM.

**Optimal configuration on Titan:** Run one model per card independently (GPU isolation mode), not one model split across three cards.

---

## 3. Software Optimization Paths (Maxwell)

These paths improve performance without new hardware. They are ordered by implementation effort and expected gain.

### 3.1 KV Cache Quantization

Quantize the KV cache to INT8 (`q8_0` in llama.cpp) instead of FP16. Reduces KV cache memory footprint by ~50%, allowing a larger model to fit within one card's 12 GB.

- **Gain:** Qwen3-8B Q4_K_M + q8_0 KV fits in ~9 GB, leaving headroom for context.
- **Status:** Supported in llama.cpp via `--cache-type-k q8_0 --cache-type-v q8_0`.
- **Quality impact:** Negligible on standard benchmarks (< 1% perplexity degradation).

### 3.2 Speculative Decoding

Pair a small draft model (e.g., Qwen3-0.6B) with the main model. The draft model proposes k tokens; the main model verifies in one forward pass. Accepted tokens are free.

- **Gain:** 2–3× throughput on typical chat workloads (acceptance rate ~60–75% with same-family draft).
- **Status:** Supported in llama.cpp (`--draft-model`). Requires VRAM for both models.
- **On Maxwell:** Draft model (0.6B) fits in ~500 MB; main model (8B Q4) ~5 GB. Total ~5.5 GB, well within 12 GB.

### 3.3 Prompt Lookup Decoding

For repetitive prompts (RAG, document Q&A), identify candidate continuations from the prompt itself and verify them as draft tokens. No separate draft model needed.

- **Gain:** 1.5–2× on document-heavy workloads.
- **Status:** Supported in llama.cpp (`--lookup-ngram-min 1`).
- **On Maxwell:** Zero additional VRAM cost.

### 3.4 GPU Isolation Mode

Instead of splitting one large model across three GPUs (tensor parallel), run one independent model instance per GPU. Route requests via a load balancer.

- **Gain:** Eliminates PCIe transfer overhead; each card runs at full GDDR5 bandwidth.
- **Trade-off:** Each card is limited to models that fit in 12 GB. Cannot run a 30B model.
- **Status:** Not yet implemented in Jar. Requires a lightweight HTTP router in front of three llama.cpp servers.

### 3.5 Asymmetric Nightly Offloading

During low-traffic hours (e.g., 02:00–06:00), offload long-running batch jobs (embedding generation, knowledge base digests) to the GPU freed from serving. During daytime, reserve GPU for interactive inference.

- **Gain:** Utilizes idle cycles without impacting latency SLA.
- **Status:** Partially implemented via `household_eval_nightly.py` in titan-node. Not yet ported to Jar.

---

## 4. Model Selection by Hardware

`jar profile` auto-selects. Manual override: set `JAR_MODEL` in the environment.

| Hardware tier | VRAM / unified RAM | Recommended model | Approx. size |
|---|---|---|---|
| AMD Strix Halo / Apple M-series (max) | 64 GB | Qwen3.6-35B-A3B (full MoE) | ~22 GB Q4 |
| RTX 4090 / 3090 | 24 GB | Qwen3-30B-A3B-Q4_K_M | ~20 GB |
| GTX TITAN X (1 card) / RTX 3080 | 12 GB | Qwen3-8B-Q4_K_M | ~5 GB |
| RTX 3060 / GTX 1080 Ti | 8–10 GB | Qwen3-8B-Q4_K_M (tight) | ~5 GB |
| Apple M2/M3 base | 16 GB | Qwen3-8B-Q4_K_M | ~5 GB |
| Apple M1 8 GB / low-end GPU | 8 GB | Qwen3-4B-Q4_K_M | ~3 GB |
| CPU-only | any | Qwen3-1.7B-Q4_K_M | ~1.1 GB |

Model files are downloaded on first `jar run` if not already present in `$JAR_HOME/models/`.

---

## 5. Sibling PC — What to Expect

If a sibling installs Jar on a typical 2022–2025 consumer PC:

- **RTX 3060 12 GB (common mid-range):** Qwen3-8B-Q4_K_M at ~25–40 tok/s decode. Very usable for chat.
- **Integrated graphics (no discrete GPU):** Falls back to CPU inference. Qwen3-1.7B is slow but functional (~2–5 tok/s on modern Ryzen/Intel).
- **Apple M-series MacBook:** Excellent. M2 Pro 16 GB runs Qwen3-8B at ~50 tok/s.

The install script detects hardware and prints the profile before downloading any model, so the sibling can confirm the recommendation before committing to a download.
