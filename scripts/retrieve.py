#!/usr/bin/env python3
"""UserPromptSubmit hook: per-prompt relevance retrieval over the memory store.

Claude Code fires UserPromptSubmit before processing each prompt and sends JSON
on stdin: { "prompt": "...", "cwd": "...", ... }. Whatever this script prints to
stdout is injected as additional context for THAT turn only.

Instead of statically loading the whole memory file (cost grows with store size),
we rank the store against the current prompt with BM25, boost by tier/votes, and
emit only the top-K. Context cost is O(K), constant regardless of store size.

Selected entries get their access_count / last_used bumped, so frequently useful
facts rise in the core ranking and stay warm against idle decay.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memstore as ms

TOP_K = 6            # max memories injected per prompt
MIN_SCORE = 0.1      # BM25 relevance floor (lexical)
EMBED_MIN = 0.30     # cosine relevance floor (semantic)
REPO_BOOST = 1.3     # project facts are usually more on-topic than global prefs
VOTE_BOOST = 0.05    # gentle tie-breaker toward reinforced facts


def vector_cache_path() -> pathlib.Path:
    return ms.claude_config_dir() / "memory.vectors.json"


def rank_lexical(prompt, cands):
    """BM25. Returns [(combined_score, relevance, entry, tier, path)]."""
    query = ms.tokenize(prompt)
    docs = [ms.tokenize(f"{e.get('fact','')} {e.get('quote','')}") for e, _, _ in cands]
    scores = ms.bm25(query, docs)
    out = []
    for (e, tier, path), rel in zip(cands, scores):
        combined = rel * (REPO_BOOST if tier == "repo" else 1.0) + VOTE_BOOST * e.get("votes", 1)
        out.append((combined, rel, e, tier, path))
    return out, MIN_SCORE


def rank_semantic(prompt, cands):
    """Embedding cosine similarity, with a persistent per-fact vector cache so
    only new/changed facts (and the prompt) are embedded each call."""
    cache = ms.load(vector_cache_path()).get("entries", {})
    if not isinstance(cache, dict):
        cache = {}
    pending, keys = [], []
    for e, _, path in cands:
        text = f"{e.get('fact','')} {e.get('quote','')}"
        key = f"{path}#{e['id']}"
        h = ms.text_hash(text)
        if cache.get(key, {}).get("h") != h:
            pending.append(text)
            keys.append((key, h))
    if pending:
        for (key, h), vec in zip(keys, ms.embed(pending)):
            cache[key] = {"h": h, "v": vec}
        ms.save(vector_cache_path(), {"entries": cache})

    qv = ms.embed([prompt])[0]
    out = []
    for e, tier, path in cands:
        v = cache.get(f"{path}#{e['id']}", {}).get("v")
        if not v:
            continue
        rel = ms.cosine(qv, v)
        combined = rel * (REPO_BOOST if tier == "repo" else 1.0) + VOTE_BOOST * e.get("votes", 1)
        out.append((combined, rel, e, tier, path))
    return out, EMBED_MIN



def candidates(cwd: str) -> list[tuple[dict, str, pathlib.Path]]:
    """Active, non-graveyard entries from global + current-repo stores.
    Returns (entry, tier, source_path) so we can write access stats back."""
    out = []
    sources = [("user", ms.global_store_path())]
    repo = ms.repo_store_path(cwd)
    if repo:
        sources.append(("repo", repo))
    for tier, path in sources:
        for e in ms.load(path).get("entries", []):
            if e.get("status") == "graveyard" or e.get("votes", 1) <= 0:
                continue
            out.append((e, tier, path))
    return out


def main() -> None:
    # Don't run inside the headless extraction call.
    if os.environ.get("MEMORY_HOOK") == "1":
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return
    prompt = (payload.get("prompt") or "").strip()
    cwd = payload.get("cwd") or os.getcwd()
    if not prompt:
        return

    cands = candidates(cwd)
    if not cands:
        return

    # Adaptive: BM25 while small, semantic embeddings once large (if installed).
    engine = ms.choose_engine(len(cands))
    try:
        ranked, floor = rank_semantic(prompt, cands) if engine == "embeddings" \
            else rank_lexical(prompt, cands)
    except Exception:
        ranked, floor = rank_lexical(prompt, cands)  # any embedding failure -> BM25

    ranked = [r for r in ranked if r[1] >= floor]
    ranked.sort(key=lambda x: x[0], reverse=True)
    picked = ranked[:TOP_K]
    if not picked:
        return

    # Emit as context for this turn.
    lines = ["Relevant remembered context for this request:"]
    for _, _, e, tier, _ in picked:
        lines.append(f"- {e['fact']}" + (f"  [{tier}]" if tier == "repo" else ""))
    sys.stdout.write("\n".join(lines) + "\n")

    # Bump access stats (warms useful facts; feeds core ranking + idle decay).
    by_path: dict[str, set] = {}
    for _, _, e, _, path in picked:
        by_path.setdefault(str(path), set()).add(e["id"])
    for path_str, ids in by_path.items():
        data = ms.load(pathlib.Path(path_str))
        for entry in data.get("entries", []):
            if entry["id"] in ids:
                entry["access_count"] = entry.get("access_count", 0) + 1
                entry["last_used"] = ms.today()
        ms.save(pathlib.Path(path_str), data)


if __name__ == "__main__":
    main()
