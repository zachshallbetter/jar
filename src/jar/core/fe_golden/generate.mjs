// Golden vectors for scripts/fe_reading.py, produced by the Fundamental engine's own built core.
// Run from titan-node:  FE_DIST=~/Projects/fundamental-engine/packages/core/dist node scripts/fe_golden/generate.mjs
// It writes scripts/fe_golden/vectors.json. Not run by the gate (the gate only reads the vectors).
import { writeFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dist = process.env.FE_DIST;
if (!dist) { console.error('set FE_DIST to fundamental-engine/packages/core/dist'); process.exit(2); }
const T = await import(pathToFileURL(join(dist, 'engine/temporal.js')).href);
const R = await import(pathToFileURL(join(dist, 'agents/relationship.js')).href);
let feCommit = 'unknown';
try { feCommit = execSync('git rev-parse --short HEAD', { cwd: dist }).toString().trim(); } catch {}

const H = T.HOUR_MS, D = T.DAY_MS, NOW = 1_791_600_000_000;   // a fixed "now" (2026-10-10)
const out = { fe_commit: feCommit, now_ms: NOW, imminence: [], freshness: [], retention: [], phase: [], relationship: [], transfer: [] };

for (const until of [-H, 0, 1, 30 * 60_000, H, 3 * H, 12 * H, D, 3 * D, 7 * D, 30 * D])
  for (const horizon of [D, 7 * D, 30 * D, 0, -1])
    out.imminence.push({ at: NOW + until, horizon, want: T.imminence(NOW + until, NOW, horizon) });
for (const since of [-D, 0, 1, H, D, 3 * D, 7 * D, 30 * D, 365 * D])
  for (const hl of [D, 7 * D, 30 * D, 0])
    out.freshness.push({ at: NOW - since, half_life: hl, want: T.freshness(NOW - since, NOW, hl) });
for (const a of [0, 0.1, 0.5, 0.9, 1, 1.5, -0.2])
  for (const since of [-D, 0, D, 7 * D, 30 * D, 180 * D])
    out.retention.push({ anchor: a, since, want: T.retention(a, since) });
for (const t of [NOW, NOW + 1, NOW - 1, NOW + 5 * H, NOW - 5 * H])
  for (const [period, offset] of [[D, 0], [7 * D, 3 * D], [D, NOW], [0, 0]])
    out.phase.push({ now: t, period, offset, want: T.phase(t, period, offset) });
// a relation exercised, then idle, then exercised again: each step's state after it
let r = { from: 'a', to: 'b', type: 'related', strength: 0, tension: 0, memory: 0, active: false };
const steps = [[true, 0.1, 0.2], [true, 0.3, 0.2], [false, 0, 0.5], [false, 0.2, 1], [true, 0.9, 0.1], [false, 2, 0.4], [true, -1, 3]];
for (const [active, tension, dt] of steps) {
  R.updateRelationship(r, active, tension, dt);
  out.relationship.push({ active, tension, dt, want: { strength: r.strength, tension: r.tension, memory: r.memory } });
}
for (const [s, att, rate] of [[0, 1, 0.25], [0.5, 0.8, 0.25], [1, 0.4, 0.5]])
  out.transfer.push({ strength: s, attention: att, rate, want: R.attentionTransfer({ strength: s }, att, rate) });

writeFileSync(join(dirname(fileURLToPath(import.meta.url)), 'vectors.json'), JSON.stringify(out, null, 1) + '\n');
console.log(`wrote vectors from FE ${feCommit}`);
