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

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
MEMORY = HERE / "memory.py"
# The detached worker has no console (stdout/stderr go to DEVNULL), so this file
# is the only window into what it did. Lives next to the global store.
LOG_FILE = pathlib.Path.home() / ".claude" / "memory-extract.log"

# Recursion guard. The llm engine runs `claude -p`, which is itself a Claude
# session whose SessionEnd fires this hook again -> fork bomb. The MEMORY_HOOK
# env var alone is not enough: Claude Code does not reliably forward it into the
# hook it spawns for the headless session. This on-disk lock does not depend on
# env propagation -- the worker holds it across the `claude -p` call, so any
# nested extraction sees it and bails. TTL guards against a stale lock left by a
# hard-killed worker (must exceed the llm engine's 120s timeout).
LOCK_FILE = pathlib.Path.home() / ".claude" / "memory-extract.lock"
LOCK_TTL = 600  # seconds

# Windows console-window suppression. The detached worker runs `claude -p`, which
# spawns its own tree of console subprocesses (node, ripgrep, its hooks). Without
# a console to inherit, each would pop a brief window. CREATE_NO_WINDOW gives the
# worker a hidden console its whole child tree inherits. 0 (no-op) on POSIX.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def log(msg: str) -> None:
    """Append a timestamped, pid-tagged line to LOG_FILE and echo to stdout.
    Stdout is visible only in the foreground hook; the file works everywhere."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [pid {os.getpid()}] {msg}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(msg)


def lock_is_active() -> bool:
    """True if a fresh extraction lock exists (another run holds it). A lock
    older than LOCK_TTL is treated as stale leftover and ignored."""
    try:
        age = time.time() - LOCK_FILE.stat().st_mtime
    except OSError:
        return False
    return age < LOCK_TTL


def acquire_lock() -> None:
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass

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
        subprocess.run(cmd, cwd=run_cwd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"[memory] store failed: {e}")


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
        log("[memory] claude CLI not found; skipping llm engine")
        return 0
    convo = "\n".join(f"{r.upper()}: {t}" for r, t in turns)[:60000]
    env = {**os.environ, "MEMORY_HOOK": "1"}  # prevents the headless call's hook from recursing
    try:
        res = subprocess.run(
            [claude, "-p", LLM_PROMPT + convo],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, creationflags=NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"[memory] llm engine failed: {e}")
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


def spawn_detached(payload: dict, engines: list[str]) -> None:
    """Fork a background copy of this script to run slow engines after the hook
    returns. SessionEnd won't wait on it, so the session closes immediately and
    the `claude -p` llm call is never cancelled mid-flight."""
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="memextract-", delete=False, encoding="utf-8"
    )
    json.dump(payload, tf)
    tf.close()
    cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), "--from-file", tf.name, *engines]
    # Cut the child loose so it survives the hook exiting. The two OS families
    # need different knobs; everything else (cmd, DEVNULL pipes) is shared.
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        # Windows: hidden console (not DETACHED_PROCESS) so the whole claude -p
        # child tree inherits it and no console windows flash; own process group
        # so the closing session's Ctrl-signals don't reach it.
        flags = NO_WINDOW or getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = flags
    else:
        # POSIX (Linux/macOS): new session so it's not killed with the parent group.
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(cmd, **kwargs)
        log(f"[memory] detached worker pid {child.pid} for engines {engines}")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log(f"[memory] detach failed, skipping background engines: {e}")
        pathlib.Path(tf.name).unlink(missing_ok=True)


def in_git_repo(cwd: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=NO_WINDOW)
        return r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


# Engines safe to run synchronously inside the hook (sub-second). Anything not
# listed is forked to a detached worker so SessionEnd never blocks on it.
FAST_ENGINES = {"regex"}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    # Recursion guard. Two independent checks so a fork bomb can't slip through:
    #   1. MEMORY_HOOK env var  - cheap, catches direct children when forwarded
    #   2. on-disk lock          - robust, works even if the env var is stripped
    if os.environ.get("MEMORY_HOOK") == "1" or lock_is_active():
        log("[memory] skip: nested or locked invocation")
        return

    argv = sys.argv[1:]

    # Detached-worker mode: payload comes from a temp file, every engine runs
    # inline (we're already backgrounded, so blocking is fine).
    detached = False
    payload_file = None
    if argv and argv[0] == "--from-file":
        detached = True
        payload_file = argv[1]
        argv = argv[2:]

    engines = argv or ["regex", "llm"]

    if detached:
        try:
            payload = json.loads(pathlib.Path(payload_file).read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            payload = {}
        finally:
            if payload_file:
                pathlib.Path(payload_file).unlink(missing_ok=True)
    else:
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

    slow = [e for e in engines if e not in FAST_ENGINES]
    if not detached and slow:
        spawn_detached(payload, slow)  # hand off llm etc. to the background

    where = "detached worker" if detached else "hook"
    run_here = engines if detached else [e for e in engines if e in FAST_ENGINES]
    log(f"[memory] {where} starting engines {run_here} over {len(turns)} turn(s)")

    total = 0
    if "regex" in run_here:
        total += engine_regex(turns, cwd)
    if "llm" in run_here:
        # Hold the lock across the `claude -p` call. The headless session's own
        # SessionEnd fires this hook again; it sees the lock and bails.
        acquire_lock()
        try:
            total += engine_llm(turns, cwd)
        finally:
            release_lock()
    log(f"[memory] {where} captured {total} fact(s) from session")


if __name__ == "__main__":
    main()
