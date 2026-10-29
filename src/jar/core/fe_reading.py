#!/usr/bin/env python3
"""The Fundamental engine's reading kernels, in Python, for the relational FE graph (#1399 P3, spec
docs/relational-fe-graph.md §6).

FE has no Python binding, and these are small closed forms, so they are ported. Each function cites
its FE source. The golden vectors in fe_golden/vectors.json are produced by FE's own built core
(fe_golden/generate.mjs), and test_fe_reading.py holds this port to them, so the two can't drift.

Every kernel is pure: no clock and no state. The caller supplies `now_ms` (epoch milliseconds, the
same unit FE uses). Degenerate inputs never produce NaN, matching FE's documented safe values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

HOUR_MS = 3_600_000   # temporal.ts HOUR_MS: imminence's softening unit
DAY_MS = 86_400_000   # temporal.ts DAY_MS: retention's τ defaults are day-calibrated


def _finite(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def clamp01(n: float) -> float:
    return 0.0 if n < 0 else 1.0 if n > 1 else float(n)


# ── world time (packages/core/src/engine/temporal.ts) ─────────────────────────────────────────────

def imminence(at_ms: float, now_ms: float, horizon_ms: float) -> float:
    """1 at or past the moment, log-ramping to 0 at the horizon's edge (temporal.ts imminence):
    clamp01(1 − ln(until/HOUR + 1) / ln(horizon/HOUR + 1))."""
    if not _finite(at_ms) or not _finite(now_ms):
        return 0.0
    until = at_ms - now_ms
    if until <= 0:
        return 1.0
    if not _finite(horizon_ms) or horizon_ms <= 0:
        return 0.0
    return clamp01(1 - math.log(until / HOUR_MS + 1) / math.log(horizon_ms / HOUR_MS + 1))


def freshness(at_ms: float, now_ms: float, half_life_ms: float) -> float:
    """2^(−since/halfLife): 1 at the moment, 0.5 one half-life later (temporal.ts freshness)."""
    if not _finite(at_ms) or not _finite(now_ms):
        return 0.0
    since = now_ms - at_ms
    if since <= 0:
        return 1.0
    if not _finite(half_life_ms) or half_life_ms <= 0:
        return 0.0
    return math.pow(2, -since / half_life_ms)


def retention(anchor: float, since_ms: float, tau_base_ms: float = 4 * DAY_MS,
              tau_growth_ms: float = 56 * DAY_MS) -> float:
    """a·e^(−since/τ(a)), τ(a) = base + a·growth: anchored facts decay slower (temporal.ts retention)."""
    a = clamp01(anchor if _finite(anchor) else 0.0)
    if not _finite(since_ms):
        return 0.0
    since = 0.0 if since_ms < 0 else since_ms
    tau = tau_base_ms + a * tau_growth_ms
    if not _finite(tau) or tau <= 0:
        return 0.0
    return a * math.exp(-since / tau)


def phase(now_ms: float, period_ms: float, offset_ms: float = 0) -> float:
    """Where now sits in a repeating period, in [0, 1) (temporal.ts phase)."""
    if not _finite(now_ms) or not _finite(offset_ms):
        return 0.0
    if not _finite(period_ms) or period_ms <= 0:
        return 0.0
    m = math.fmod(now_ms - offset_ms, period_ms)   # JS % keeps the dividend's sign, as fmod does
    f = (m + period_ms if m < 0 else m) / period_ms
    return 0.0 if f >= 1 else f


# ── relationships (packages/core/src/agents/relationship.ts) ──────────────────────────────────────

@dataclass
class Relationship:
    strength: float = 0.0
    tension: float = 0.0
    memory: float = 0.0
    active: bool = False


STRENGTHEN, DECAY, REMEMBER = 1.5, 0.3, 0.2   # DEFAULT_RELATIONSHIP_DYNAMICS


def update_relationship(r: Relationship, active: bool, tension: float, dt: float,
                        strengthen: float = STRENGTHEN, decay: float = DECAY, remember: float = REMEMBER) -> Relationship:
    """One step: strengthens while exercised, decays while idle; memory follows more slowly, so a
    long-used relation stays warm after a pause (relationship.ts updateRelationship). Mutates and returns r."""
    r.active = active
    r.tension = clamp01(tension)
    r.strength = clamp01(r.strength + (strengthen if active else -decay) * dt)
    r.memory = clamp01(r.memory + (remember if active else -remember * 0.5) * dt)
    return r


def attention_transfer(r: Relationship, from_attention: float, rate: float = 0.25) -> float:
    """The attention that flows across a relation (relationship.ts attentionTransfer)."""
    return from_attention * r.strength * rate


# ── relational metrics (packages/dom/src/metrics.ts computeMetrics, the relation lanes) ───────────

def coherence(resolved: int, conflict: int, total: int) -> float | None:
    """resolved/total − conflict/total; None with no relations (FE keeps the previous value)."""
    if total <= 0:
        return None
    return clamp01(resolved / total - conflict / total)


def entropy(resolved: int, conflict: int, total: int) -> float | None:
    """conflict/total + (1 − resolved/total)·0.5: contested or unsettled evidence; None with none."""
    if total <= 0:
        return None
    return clamp01(conflict / total + (1 - resolved / total) * 0.5)


def pressure(entropy_v: float, recency: float) -> float:
    """entropy·0.6 + (1 − recency)·0.4: what presses for attention (metrics.ts pressure)."""
    return clamp01(entropy_v * 0.6 + (1 - recency) * 0.4)


# ── conserved attention (docs/engine-reference/forces-formulas.md §3.7) ────────────────────────────

def conserved_share(demands: list[float], budget: float) -> list[float]:
    """A fixed budget split in proportion to demand: d_i = N · m_i / Σm. All zero when there's no demand."""
    total = sum(m for m in demands if _finite(m) and m > 0)
    if total <= 0 or not _finite(budget) or budget <= 0:
        return [0.0] * len(demands)
    return [budget * (m / total if _finite(m) and m > 0 else 0.0) for m in demands]


def top_k(scores: dict[str, float], budget: float, k: int) -> list[tuple[str, float]]:
    """The k largest shares of the budget, strongest first, ties by key (deterministic)."""
    keys = sorted(scores)
    shares = conserved_share([scores[x] for x in keys], budget)
    ranked = sorted(zip(keys, shares), key=lambda p: (-p[1], p[0]))
    return [p for p in ranked[:max(0, k)] if p[1] > 0]
