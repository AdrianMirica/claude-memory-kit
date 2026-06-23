#!/usr/bin/env python3
"""Shared helpers for the memory layer: path resolution, store IO, tokenization,
BM25 ranking, and the core-relevance score. Imported by retrieve.py (and usable
by memory.py). Pure standard library.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import subprocess

# Decay / ranking knobs (kept in sync with memory.py).
TTL_DAYS = 90       # since last_validated before a fact needs re-validation
IDLE_DAYS = 120     # cold + low-value facts past this are auto-graveyarded
CORE_MAX = 20       # max entries written to the always-on active file (per tier)
W_VOTES = 1.0
W_RECENCY = 2.0
W_ACCESS = 0.5

# Adaptive retrieval: under the threshold use BM25 (free, stdlib); at/above it
# auto-upgrade to semantic embeddings IF available, else fall back to BM25.
EMBED_THRESHOLD = int(os.environ.get("MEMORY_EMBED_THRESHOLD", "200"))
_EMBED_MODEL = None

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "is",
    "are", "was", "were", "be", "been", "this", "that", "these", "those", "it",
    "its", "i", "you", "we", "they", "he", "she", "do", "does", "did", "how",
    "what", "when", "where", "why", "which", "with", "as", "at", "by", "from",
    "into", "about", "should", "would", "could", "can", "will", "my", "our",
    "your", "me", "us", "so", "if", "then", "than", "not", "no", "yes", "have",
    "has", "had", "get", "got", "use", "using", "via", "here", "there",
}


def today() -> str:
    return dt.date.today().isoformat()


def days_since(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        return (dt.date.today() - dt.date.fromisoformat(iso)).days
    except ValueError:
        return 0


def claude_config_dir() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or (pathlib.Path.home() / ".claude")
    )


def git_root(cwd: str | os.PathLike | None) -> pathlib.Path | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or "."), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return pathlib.Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def global_store_path() -> pathlib.Path:
    return claude_config_dir() / "memory.store.json"


def repo_store_path(cwd: str | os.PathLike | None) -> pathlib.Path | None:
    root = git_root(cwd)
    return (root / ".claude" / "memory.store.json") if root else None


def load(path: pathlib.Path) -> dict:
    if not path or not path.exists():
        return {"entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {"entries": []}
    data.setdefault("entries", [])
    return data


def save(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP]


def core_score(e: dict) -> float:
    """Global relevance used to pick the always-on core (A1) — votes, recency, use."""
    votes = e.get("votes", 1)
    age = days_since(e.get("last_validated") or e.get("added"))
    recency = max(0.0, 1.0 - age / TTL_DAYS)
    access = e.get("access_count", 0)
    return W_VOTES * votes + W_RECENCY * recency + W_ACCESS * access


def bm25(query: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Standard BM25 over pre-tokenized docs. Returns one score per doc."""
    n = len(docs)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    q = set(query)
    scores = []
    for d in docs:
        dl = len(d) or 1
        freq: dict[str, int] = {}
        for t in d:
            freq[t] = freq.get(t, 0) + 1
        s = 0.0
        for t in q:
            f = freq.get(t, 0)
            if not f:
                continue
            ni = df.get(t, 0)
            idf = math.log(1 + (n - ni + 0.5) / (ni + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def text_hash(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]


# ---- adaptive engine selection + semantic (embedding) retrieval ----

def embed_available() -> bool:
    """True if a local embedding backend is installed (no API key needed)."""
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


def choose_engine(n_candidates: int) -> str:
    """Pick 'bm25' or 'embeddings' adaptively. Honors MEMORY_ENGINE override
    ('bm25' | 'embeddings' | 'auto'). In auto mode, embeddings activate only once
    the store is large AND the backend is installed — otherwise BM25."""
    forced = os.environ.get("MEMORY_ENGINE", "auto").lower()
    if forced == "bm25":
        return "bm25"
    if forced == "embeddings":
        return "embeddings" if embed_available() else "bm25"
    # auto: short-circuits so fastembed is never imported while the store is small
    if n_candidates >= EMBED_THRESHOLD and embed_available():
        return "embeddings"
    return "bm25"


def _model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        from fastembed import TextEmbedding
        _EMBED_MODEL = TextEmbedding()  # downloads a small model on first use
    return _EMBED_MODEL


def embed(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in _model().embed(list(texts))]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
