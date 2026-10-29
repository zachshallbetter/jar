"""Hardware detection and model profile recommendation for Jar nodes."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict


def detect_platform() -> Dict[str, Any]:
    return {
        "os": sys.platform,
        "platform_system": platform.system(),
        "arch": platform.machine(),
        "processor": platform.processor(),
    }


def detect_memory() -> Dict[str, Any]:
    system = platform.system()
    total_bytes = 0

    if system == "Darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            total_bytes = int(out)
        except Exception:
            pass
    elif system == "Linux":
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split()
                        total_bytes = int(parts[1]) * 1024
                        break
        except Exception:
            pass

    if total_bytes == 0:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            total_bytes = pages * page_size
        except Exception:
            total_bytes = 16 * 1024 * 1024 * 1024

    ram_gb = round(total_bytes / (1024 ** 3), 1)
    return {
        "ram_bytes": total_bytes,
        "ram_gb": ram_gb,
    }


def detect_accelerators() -> Dict[str, Any]:
    system = platform.system()
    accelerators = []
    gpu_vram_gb = 0.0

    if system == "Darwin" and platform.machine() in ("arm64", "aarch64"):
        accelerators.append({
            "type": "apple_silicon_metal",
            "name": "Apple Silicon (Metal MPS)",
            "unified_memory": True,
        })

    if shutil.which("nvidia-smi"):
        try:
            cmd = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
            if out:
                for idx, line in enumerate(out.splitlines()):
                    parts = [p.strip() for p in line.split(",")]
                    name = parts[0]
                    vram_mb = float(parts[1]) if len(parts) > 1 else 0.0
                    vram_gb = round(vram_mb / 1024, 1)
                    gpu_vram_gb += vram_gb
                    accelerators.append({
                        "type": "nvidia_cuda",
                        "index": idx,
                        "name": name,
                        "vram_gb": vram_gb,
                        "unified_memory": False,
                    })
        except Exception:
            pass

    if not accelerators:
        accelerators.append({
            "type": "cpu_only",
            "name": "Host CPU",
            "unified_memory": False,
        })

    return {
        "devices": accelerators,
        "total_dedicated_vram_gb": round(gpu_vram_gb, 1),
    }


def recommend_profile(mem: Dict[str, Any], acc: Dict[str, Any]) -> Dict[str, Any]:
    ram_gb = mem.get("ram_gb", 16.0)
    devices = acc.get("devices", [])
    has_apple_metal = any(d.get("type") == "apple_silicon_metal" for d in devices)
    has_nvidia = any(d.get("type") == "nvidia_cuda" for d in devices)
    dedicated_vram = acc.get("total_dedicated_vram_gb", 0.0)

    if has_apple_metal:
        effective_memory = ram_gb * 0.70
        backend = "metal"
    elif has_nvidia:
        effective_memory = dedicated_vram
        backend = "cuda"
    else:
        effective_memory = ram_gb * 0.50
        backend = "cpu"

    if effective_memory >= 40.0:
        tier = "heavy"
        recommended_model = "Qwen3.6-35B-A3B-Q4_K_M"
        context_size = 32768
        description = "High-capability MoE (35B active parameters) + 32k context + full local embeddings & vision."
    elif effective_memory >= 12.0:
        tier = "standard"
        recommended_model = "Qwen3-8B-Q4_K_M"
        context_size = 32768
        description = "Standard household intelligence: 8B generalist brain with 32k context window."
    elif effective_memory >= 6.0:
        tier = "light"
        recommended_model = "Qwen2.5-3B-Instruct-Q4_K_M"
        context_size = 16384
        description = "Fast, lightweight household agent for budget hardware and mini-PCs (3B model)."
    else:
        tier = "minimal"
        recommended_model = "Qwen2.5-1.5B-Instruct-Q4_K_M"
        context_size = 8192
        description = "Minimal footprint agent for constrained systems (<8 GB RAM)."

    return {
        "tier": tier,
        "backend": backend,
        "effective_memory_gb": round(effective_memory, 1),
        "recommended_model": recommended_model,
        "recommended_context": context_size,
        "description": description,
    }


def inspect_all() -> Dict[str, Any]:
    plat = detect_platform()
    mem = detect_memory()
    acc = detect_accelerators()
    rec = recommend_profile(mem, acc)
    return {
        "platform": plat,
        "memory": mem,
        "accelerators": acc,
        "recommendation": rec,
    }

