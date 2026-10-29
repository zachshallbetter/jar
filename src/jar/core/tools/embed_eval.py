"""Compare embedding setups on the live KB: for each question, rank the notes by cosine
similarity and check where the expected note lands (hit@1, hit@3, MRR).

Run on titan (agent env sourced). A candidate model needs its own llama-server, e.g.
  CUDA_VISIBLE_DEVICES=2 /ai/tools/llama.cpp-b11259/llama-server --port 18016 \
    --model /ai/models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf \
    --embedding --pooling last --ctx-size 8192 --parallel 4 --batch-size 2048 --ubatch-size 2048
Results and the decision: docs/evals/embeddings-2026-09-29.md. Re-run with real household
questions once the KB holds live data (calendar, scans), before switching models."""
import json, math, sys, urllib.request
sys.path.insert(0, "/ai/projects/titan-node/scripts")
import kb_hygiene
Q = [
 ("how do I restart a service on titan", "Managing Titan services"),
 ("which systemd units run on the server", "Titan systemd services — complete list"),
 ("what GPUs are in titan and which model runs on each", "Titan GPU and model details"),
 ("where is the agent's config file on the host", "Titan file system layout"),
 ("how do the runtimes check the agent's token", "Coordinator JWT authentication for runtimes"),
 ("what mistakes have caused outages before", "Titan hard rules — things that have burned us"),
 ("the agent returns empty replies, how do I fix it", "Titan troubleshooting — common failures and fixes"),
 ("which titan.sh commands are there", "Titan CLI — titan.sh subcommands"),
 ("how do I ship a script change to titan", "Development workflow — scripting on Titan from the Mac"),
 ("how do I connect to titan over ssh", "Connecting to Titan via SSH"),
 ("which postgres database does the record runtime use", "Titan database — Postgres and pgvector"),
 ("what is jellybean", "Jellybean App"),
 ("how does the mac app run device actions for the agent", "Device bridge — how it works"),
 ("which device tools can the agent use on my mac", "Device tools — complete list"),
 ("what does the field scanner track about my apps", "OSFieldScanner — what it tracks"),
 ("how should the agent interpret dwell time and focus", "Field state interpretation guide"),
 ("when does jellybean show proactive cards", "Proactive card system"),
 ("what's playing on plex", "Plex Media Server integration"),
 ("what version of jellybean is current", "Jellybean development — current branch and status"),
 ("how is the jellybean app structured internally", "Jellybean app — detailed architecture"),
 ("what is in the situation block each turn", "What [SITUATION] contains"),
 ("how does the agent remember things long term", "Memory architecture"),
 ("how should the agent phrase its answers", "Behavioral priming — response style"),
 ("when should the agent call a tool instead of guessing", "Tool calling patterns"),
]
def embed(url, texts):
    out = []
    for i in range(0, len(texts), 8):
        body = json.dumps({"input": texts[i:i+8]}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}), timeout=120))
        out += [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    return out
def cos(a, b):
    return sum(x*y for x, y in zip(a, b)) / math.sqrt(sum(x*x for x in a) * sum(y*y for y in b))
srcs = [s for s in kb_hygiene.list_sources()]
notes = [(s.get("title",""), kb_hygiene.source_text(s["source_id"])[:4000]) for s in srcs]
titles = [t for t, _ in notes]
missing = [e for _, e in Q if e not in titles]
if missing: print("expected titles missing:", missing)
setups = {
 "nomic, as today (no prefixes)": ("http://127.0.0.1:8015/v1/embeddings", lambda d: d, lambda q: q),
 "nomic, with its prefixes": ("http://127.0.0.1:8015/v1/embeddings", lambda d: "search_document: " + d, lambda q: "search_query: " + q),
 "qwen3-0.6b, official instruction, title + first 1500 chars": ("http://127.0.0.1:18016/v1/embeddings",
     lambda d: d.split("\n")[0] + "\n" + d[:1500],
     lambda q: "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:" + q),
}
for name, (url, fd, fq) in setups.items():
    D = embed(url, [fd(f"{t}\n{x}") for t, x in notes]); QE = embed(url, [fq(q) for q, _ in Q])
    h1 = h3 = mrr = 0
    for (q, exp), qe in zip(Q, QE):
        ranked = [titles[i] for i in sorted(range(len(D)), key=lambda i: -cos(qe, D[i]))]
        r = ranked.index(exp) + 1 if exp in ranked else 99
        h1 += r == 1; h3 += r <= 3; mrr += 1 / r
    n = len(Q); print(f"{name:<34} dim={len(D[0]):<5} hit@1 {h1}/{n} ({100*h1/n:.0f}%)  hit@3 {h3}/{n} ({100*h3/n:.0f}%)  MRR {mrr/n:.3f}")
