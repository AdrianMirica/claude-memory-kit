#!/usr/bin/env python3
"""SessionEnd extractor: sweeps a finished Claude Code conversation for durable
facts and persists them via memory.py (which dedupes + rebuilds the active file).

Wired as a Claude Code `SessionEnd` hook. Claude Code sends JSON on stdin, e.g.
  { "transcript_path": "/path/session.jsonl", "cwd": "/path/to/repo", ... }

Two engines:
  regex  - zero-cost phrase matcher for explicit user preferences  -> global tier
  llm    - one `claude -p` call that extracts decisions / tech-stack /
           issue->solution / conventions, classified by tier        -> repo or global

Recursion guard: the llm engine sets MEMORY_HOOK=1 before calling `claude`, and
this script exits immediately if it sees MEMORY_HOOK already set. That stops the
headless call's own SessionEnd from re-triggering extraction forever.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
MEMORY = HERE / "memory.py"

# Explicit-preference triggers for the cheap engine (user-scope / global).
TRIGGERS = re.compile(
    r"\b(remember(?: that)?|from now on|going forward|always|never|"
    r"i prefer|i (?:usually|generally) (?:use|like)|make sure to)\b",
    re.IGNORECASE,
)

LLM_PROMPT = """You are a memory extractor. Read the conversation transcript and output ONLY
durable, reusable facts worth remembering for future sessions. Capture:
- user preferences / workflow habits
- technical stack choices and the reason
- architecture or flow decisions
- issue -> root cause -> solution pairs
- conventions the user stated or you verified

Do NOT include: task-specific/one-off instructions, transient state, secrets,
tokens, or personal sensitive data. Keep each fact under 200 characters.

Classify scope:
- "repo"  : facts about THIS project (stack, flows, bugs/fixes, conventions)
- "user"  : personal preferences that apply across all projects

Return ONLY a JSON array, no prose:
[{"fact": "...", "scope": "repo|user", "evidence": "short quote or pointer"}]

Transcript:
"""


def read_transcript(path: str) -> list[tuple[str, str]]:
    """Return [(role, text)] from a Claude Code JSONL transcript."""
    out: list[tuple[str, str]] = []
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message", obj)
        role = msg.get("role") or obj.get("type", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            out.append((role, content.strip()))
    return out


def store(fact: str, scope: str, evidence: str, cwd: str) -> None:
    """Persist one fact via memory.py (handles dedupe + active-file rebuild)."""
    cmd = [sys.executable, str(MEMORY)]
    run_cwd = cwd or os.getcwd()
    if scope == "user":
        cmd.append("--global")
    cmd += ["add", fact[:200], "--scope", scope, "--quote", evidence[:200]]
    env = {**os.environ, "MEMORY_HOOK": "1"}  # guard nested invocations
    try:
        subprocess.run(cmd, cwd=run_cwd, env=env, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[memory] store failed: {e}", file=sys.stderr)


def engine_regex(turns: list[tuple[str, str]], cwd: str) -> int:
    n = 0
    for role, text in turns:
        if role not in ("user", "human"):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
            s = sentence.strip()
            if s and TRIGGERS.search(s) and len(s) < 200:
                store(s, "user", s, cwd)
                n += 1
    return n


def engine_llm(turns: list[tuple[str, str]], cwd: str) -> int:
    claude = shutil.which("claude")
    if not claude:
        print("[memory] claude CLI not found; skipping llm engine", file=sys.stderr)
        return 0
    convo = "\n".join(f"{r.upper()}: {t}" for r, t in turns)[:60000]
    env = {**os.environ, "MEMORY_HOOK": "1"}  # prevents the headless call's hook from recursing
    try:
        res = subprocess.run(
            [claude, "-p", LLM_PROMPT + convo],
            env=env, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[memory] llm engine failed: {e}", file=sys.stderr)
        return 0
    facts = parse_json_array(res.stdout)
    n = 0
    for f in facts:
        fact = (f.get("fact") or "").strip()
        scope = "repo" if f.get("scope") == "repo" else "user"
        if scope == "repo" and not in_git_repo(cwd):
            scope = "user"  # no repo context -> keep it global
        if fact:
            store(fact, scope, (f.get("evidence") or "").strip() or fact, cwd)
            n += 1
    return n


def parse_json_array(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def in_git_repo(cwd: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
        return r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> None:
    # Recursion guard: if invoked from within an llm extraction call, do nothing.
    if os.environ.get("MEMORY_HOOK") == "1":
        return

    engines = sys.argv[1:] or ["regex", "llm"]  # default: both
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    transcript = payload.get("transcript_path", "")
    cwd = payload.get("cwd", os.getcwd())
    if not transcript:
        return

    turns = read_transcript(transcript)
    if not turns:
        return

    total = 0
    if "regex" in engines:
        total += engine_regex(turns, cwd)
    if "llm" in engines:
        total += engine_llm(turns, cwd)
    print(f"[memory] captured {total} fact(s) from session")


if __name__ == "__main__":
    main()
