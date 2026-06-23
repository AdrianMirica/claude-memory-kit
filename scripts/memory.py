#!/usr/bin/env python3
"""Dynamic-memory layer for tools that only load a static markdown file.

Pipeline:  memory.store.json  --(validate)-->  memory.active.md  --(@import)-->  CLAUDE.md

The store is the source of truth. The active file is a *generated cache* that
contains only entries whose code citations still hold on the current branch.
The host tool (Claude Code) stays dumb: it just imports the active file.

Subcommands
  build            Validate citations, decay stale entries, write memory.active.md
  add              Append a new entry (auto-anchors a code citation if given)
  vote <id> +|-    Reinforce / weaken an entry; <=0 votes is garbage-collected
  gc               Move stale / needs_revalidation / dead entries to the graveyard

Citation anchoring uses a content hash of the cited lines (dependency-free,
tool-agnostic). If the cited lines change, the entry is demoted to
`needs_revalidation` and withheld from the active file until a human re-blesses
it (re-run `add`/edit + `build`, or bump last_validated).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memstore as ms

# Resolved per-invocation in main() based on --global. Repo tier anchors to the
# current git repo; global tier lives in ~/.claude and applies to every repo.
ROOT: pathlib.Path = pathlib.Path.cwd()
STORE: pathlib.Path = ROOT / ".claude" / "memory.store.json"
ACTIVE: pathlib.Path = ROOT / ".claude" / "memory.active.md"
IS_GLOBAL = False
# Single source of truth for decay/ranking knobs lives in memstore.
TTL_DAYS = ms.TTL_DAYS
IDLE_DAYS = ms.IDLE_DAYS
CORE_MAX = ms.CORE_MAX
CITE_RE = re.compile(r"^(?P<path>[^:]+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def git_root() -> pathlib.Path:
    """Top level of the current git repo, or cwd if not in one."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return pathlib.Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return pathlib.Path.cwd()


def claude_config_dir() -> pathlib.Path:
    """Claude Code's config dir, cross-platform. CLAUDE_HOME wins (used by tests),
    then Claude Code's own CLAUDE_CONFIG_DIR, else ~/.claude."""
    return pathlib.Path(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or (pathlib.Path.home() / ".claude")
    )


def configure(is_global: bool) -> None:
    """Point STORE/ACTIVE/ROOT at the global (~/.claude) or repo tier."""
    global ROOT, STORE, ACTIVE, IS_GLOBAL
    IS_GLOBAL = is_global
    base = claude_config_dir() if is_global else (git_root() / ".claude")
    ROOT = base.parent if is_global else git_root()
    base.mkdir(parents=True, exist_ok=True)
    STORE = base / "memory.store.json"
    ACTIVE = base / "memory.active.md"


def today() -> str:
    return dt.date.today().isoformat()


def is_idle(e: dict) -> bool:
    """Cold (unused/unvalidated past IDLE_DAYS) and never reinforced (A2)."""
    last = e.get("last_used") or e.get("last_validated") or e.get("added")
    return ms.days_since(last) > IDLE_DAYS and e.get("votes", 1) <= 1


def load() -> dict:
    if not STORE.exists():
        return {"entries": []}
    data = json.loads(STORE.read_text(encoding="utf-8") or "{}")
    data.setdefault("entries", [])
    return data


def save(data: dict) -> None:
    STORE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cited_lines(cite: str) -> str | None:
    """Return the exact text of the cited line range, or None if unresolved."""
    m = CITE_RE.match(cite.strip())
    if not m:
        return None
    path = ROOT / m.group("path")
    if not path.exists():
        return None
    start = int(m.group("start"))
    end = int(m.group("end") or start)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if start < 1 or end > len(lines):
        return None
    return "\n".join(lines[start - 1 : end])


def anchor(cite: str) -> str | None:
    text = cited_lines(cite)
    if text is None:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def validate(entry: dict) -> str:
    """Return a status: active | needs_revalidation | stale."""
    cite = entry.get("cite")
    # Global/user memory is not tied to any repo branch -> no code anchor; it
    # relies on quote + votes + TTL. Repo facts validate cited lines vs branch.
    if cite and not IS_GLOBAL:
        current = anchor(cite)
        if current is None:
            return "stale"  # file or lines gone
        if entry.get("cite_hash") and current != entry["cite_hash"]:
            return "needs_revalidation"  # cited code drifted
        entry["cite_hash"] = current
    # Time-based decay.
    last = entry.get("last_validated", entry.get("added", today()))
    try:
        age = (dt.date.today() - dt.date.fromisoformat(last)).days
    except ValueError:
        age = 0
    if age > TTL_DAYS:
        return "needs_revalidation"
    return "active"


def cmd_build(_: argparse.Namespace) -> None:
    data = load()
    repo, user, withheld = [], [], []
    for e in data["entries"]:
        if e.get("status") == "graveyard":
            continue
        if is_idle(e):  # A2: prune cold, never-reinforced facts
            e["status"] = "graveyard"
            withheld.append((e, "idle"))
            continue
        status = validate(e)
        e["status"] = status
        if status == "active":
            e["last_validated"] = today()
            (user if e.get("scope") == "user" else repo).append(e)
        else:
            withheld.append((e, status))
    save(data)

    # A1: emit only the top-CORE_MAX by relevance score, not everything.
    repo_core = sorted(repo, key=ms.core_score, reverse=True)[:CORE_MAX]
    user_core = sorted(user, key=ms.core_score, reverse=True)[:CORE_MAX]

    out = ["<!-- GENERATED by scripts/memory.py — do not edit; edit memory.store.json -->",
           f"<!-- built {today()}; always-on core (top {CORE_MAX}/tier). "
           "Topical facts are retrieved per-prompt by retrieve.py -->", ""]
    if repo_core:
        out.append("## Repository facts")
        out += [_render(e) for e in repo_core]
        out.append("")
    if user_core:
        out.append("## User preferences")
        out += [_render(e) for e in user_core]
    ACTIVE.write_text("\n".join(out) + "\n", encoding="utf-8")

    # Diagnostics go to stderr so SessionStart doesn't inject build noise as context.
    capped = (len(repo) - len(repo_core)) + (len(user) - len(user_core))
    print(f"active core: {len(repo_core)} repo + {len(user_core)} user"
          + (f" ({capped} held back for on-demand retrieval)" if capped else ""),
          file=sys.stderr)
    for e, s in withheld:
        print(f"withheld [{s}] {e['id']}: {e['fact'][:60]} (cite={e.get('cite','-')})",
              file=sys.stderr)

    maybe_suggest_embeddings(data, len(repo) + len(user))
    maybe_suggest_llm(data)


def _nudge(data: dict, key: str, msg: str) -> None:
    """Emit a one-time [memory-setup] hint. stdout -> relayed by Claude via the
    SessionStart hook; stderr -> visible on manual runs. Shown once per store."""
    if data.get(key):
        return
    print(msg)
    print(msg, file=sys.stderr)
    data[key] = True
    save(data)


def maybe_suggest_embeddings(data: dict, n_candidates: int) -> None:
    """Nudge to install a semantic backend once the store outgrows keyword search."""
    if n_candidates < ms.EMBED_THRESHOLD or ms.embed_available():
        return
    _nudge(data, "_embed_hint_shown",
           f"[memory-setup] Your memory store has {n_candidates} facts - past the "
           f"point where keyword search starts missing relevant ones. Install a "
           f"local semantic backend to auto-upgrade retrieval: `pip install fastembed`. "
           f"No API key, no config change; it activates automatically.")


def _session_end_has_llm(cfg_dir: pathlib.Path):
    """True/False if the SessionEnd extract hook includes the 'llm' engine,
    or None if it can't be determined (no hook / unreadable settings)."""
    try:
        settings = json.loads((cfg_dir / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for group in settings.get("hooks", {}).get("SessionEnd", []):
        for h in group.get("hooks", []):
            args = [str(a).lower() for a in h.get("args", [])]
            if any("extract.py" in a for a in args):
                return "llm" in args
    return None


def maybe_suggest_llm(data: dict) -> None:
    """Nudge to enable richer session-end capture when the `claude` CLI is present
    but the SessionEnd hook is running keyword-only."""
    if not IS_GLOBAL or not shutil.which("claude"):
        return
    if _session_end_has_llm(ms.claude_config_dir()) is not False:
        return  # llm already on, or config unknown -> don't nudge
    _nudge(data, "_llm_hint_shown",
           "[memory-setup] The `claude` CLI is installed but session-end capture is "
           "running keyword-only, so it misses decisions, tech-stack choices, and "
           "bug-fix notes. Enable richer capture by adding the 'llm' engine to your "
           "SessionEnd hook args (or re-run install.py to restore the default).")


def _render(e: dict) -> str:
    src = f"  _src: {e['cite']}_" if e.get("cite") else ""
    return f"- {e['fact']}{src}"


def cmd_add(a: argparse.Namespace) -> None:
    data = load()
    scope = a.scope or ("user" if IS_GLOBAL else "repo")
    if a.cite and IS_GLOBAL:
        sys.exit("Global memory can't use --cite (not tied to a repo). Use --quote.")
    eid = "m-" + hashlib.sha1((a.fact + today()).encode()).hexdigest()[:6]
    entry = {
        "id": eid,
        "fact": a.fact,
        "scope": scope,
        "votes": 1,
        "added": today(),
        "last_validated": today(),
        "access_count": 0,
        "status": "active",
    }
    if a.cite:
        entry["cite"] = a.cite
        h = anchor(a.cite)
        if h is None:
            sys.exit(f"Cannot anchor citation '{a.cite}' (path/lines not found)")
        entry["cite_hash"] = h
    if a.quote:
        entry["quote"] = a.quote
    # Dedupe on fact text.
    for e in data["entries"]:
        if e["fact"].strip().lower() == a.fact.strip().lower():
            e["votes"] = e.get("votes", 1) + 1
            e["last_validated"] = today()
            save(data)
            print(f"duplicate -> voted up {e['id']} (votes={e['votes']})")
            cmd_build(a)
            return
    data["entries"].append(entry)
    save(data)
    print(f"added {eid}")
    cmd_build(a)  # auto-regenerate the active file Claude loads


def cmd_vote(a: argparse.Namespace) -> None:
    data = load()
    for e in data["entries"]:
        if e["id"] == a.id:
            e["votes"] = e.get("votes", 1) + (1 if a.dir == "+" else -1)
            if e["votes"] <= 0:
                e["status"] = "graveyard"
                print(f"{a.id} votes={e['votes']} -> graveyard")
            else:
                e["last_validated"] = today()
                print(f"{a.id} votes={e['votes']}")
            save(data)
            cmd_build(a)
            return
    sys.exit(f"no entry {a.id}")


def cmd_gc(_: argparse.Namespace) -> None:
    data = load()
    moved = 0
    for e in data["entries"]:
        if e.get("status") in ("stale", "needs_revalidation") and e.get("votes", 1) <= 0:
            e["status"] = "graveyard"
            moved += 1
    save(data)
    print(f"graveyard += {moved}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--global", dest="is_global", action="store_true",
                   help="operate on the cross-session global store (~/.claude) instead of the repo store")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build").set_defaults(func=cmd_build)

    pa = sub.add_parser("add")
    pa.add_argument("fact")
    pa.add_argument("--scope", choices=["repo", "user"], default=None,
                    help="default: 'user' with --global, else 'repo'")
    pa.add_argument("--cite", help="path:start-end relative to repo root (repo tier only)")
    pa.add_argument("--quote", help="verbatim user quote (for user prefs)")
    pa.set_defaults(func=cmd_add)

    pv = sub.add_parser("vote")
    pv.add_argument("id")
    pv.add_argument("dir", choices=["+", "-"])
    pv.set_defaults(func=cmd_vote)

    sub.add_parser("gc").set_defaults(func=cmd_gc)

    args = p.parse_args()
    configure(args.is_global)
    args.func(args)


if __name__ == "__main__":
    main()
