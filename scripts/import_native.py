#!/usr/bin/env python3
"""One-shot seeder: import Claude Code's native memory-tool notes into the kit store.

Claude Code's filesystem memory tool writes free-form markdown under
  ~/.claude/projects/<encoded-repo>/memory/**/*.md   (project-scoped notes)
  ~/.claude/memory/**/*.md                            (global/user notes, if any)

retrieve.py only reads `memory.store.json`, so those notes are invisible to the
kit. This script distills bullet lines from those .md files and persists them via
memory.py (reused as a library) so you get dedupe + a single active-file rebuild.

It does NOT touch embeddings. fastembed is a retrieval-ranking upgrade that only
activates at >=200 facts; seeding just appends entries to the JSON store.

Scope mapping
  ~/.claude/memory/**            -> user (global ~/.claude store)
  ~/.claude/projects/<enc>/memory/** -> repo, IF the encoded path resolves to a
       local git repo; otherwise skipped (or --unresolved global to keep them).

Safe by default: prints a preview and writes nothing unless you pass --apply.

Usage
  python scripts/import_native.py                 # dry-run preview
  python scripts/import_native.py --apply         # actually write
  python scripts/import_native.py --apply --unresolved global
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory as mem  # reused as a library (its main() is __main__-guarded)
import memstore as ms

MAX_FACT_LEN = 200          # match the kit's convention (extract.py truncates to 200)
MIN_FACT_LEN = 12           # drop trivially short fragments
_KV_RE = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$")


def claude_dir() -> pathlib.Path:
    return ms.claude_config_dir()


def decode_project_dir(name: str) -> pathlib.Path | None:
    """Resolve an encoded project-dir name back to a real local path.

    Claude encodes paths by replacing separators (and ':') with '-', which is
    lossy because folder names may contain '-'. We disambiguate against the
    filesystem with a longest-match, backtracking resolver so multi-dash folder
    names resolve correctly. Returns None if no existing path matches.
    """
    m = re.match(r"^([A-Za-z])--(.*)$", name)        # Windows: 'C--Work-...' -> C:\Work\...
    if m:
        base = pathlib.Path(f"{m.group(1)}:\\")
        rest = m.group(2)
    else:                                            # POSIX: leading '-' was '/'
        base = pathlib.Path("/")
        rest = name.lstrip("-")
    tokens = [t for t in rest.split("-") if t != ""]

    def resolve(cur: pathlib.Path, toks: list[str]) -> pathlib.Path | None:
        if not toks:
            return cur if cur.is_dir() else None
        for k in range(len(toks), 0, -1):            # longest segment first
            nxt = cur / "-".join(toks[:k])
            if nxt.is_dir():
                got = resolve(nxt, toks[k:])
                if got is not None:
                    return got
        return None

    return resolve(base, tokens)


def is_git_repo(path: pathlib.Path) -> bool:
    return (path / ".git").exists()


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        v = v[1:-1]
    return v.strip()


def parse_memory_file(md: pathlib.Path) -> tuple[str, str] | None:
    """Parse one native memory file (YAML-frontmatter + rationale body).

    Each file is a single memory. The `description` field is the durable, concise
    fact we store; the body is rationale we keep only as short provenance. Falls
    back to `name` (humanized) or the first body paragraph if description is absent.

    Returns (fact, origin_name) or None.
    """
    try:
        raw = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()

    # Drop a leading fence/info tag some exports add (``` or ```yaml or bare 'yaml').
    while lines and (not lines[0].strip() or lines[0].strip().lower() in ("yaml", "```yaml", "```", "---")):
        lines.pop(0)

    name = desc = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":            # end of an explicit frontmatter block
            body_start = i + 1
            break
        m = _KV_RE.match(line)
        if m:
            key, val = m.group(1).lower(), m.group(2)
            if key == "name" and not name:
                name = _unquote(val)
            elif key == "description" and not desc:
                desc = _unquote(val)
            body_start = i + 1
            continue
        if line.strip() == "":               # blank line ends the header region
            body_start = i + 1
            break
        body_start = i                        # first real prose line -> body begins
        break

    fact = desc.strip()
    if len(fact) < MIN_FACT_LEN:
        body = "\n".join(lines[body_start:]).strip()
        para = next((re.sub(r"\s+", " ", p).strip()
                     for p in re.split(r"\n\s*\n", body) if p.strip()), "")
        fact = para or name.replace("-", " ").strip()
    if len(fact) < MIN_FACT_LEN:
        return None
    return fact[:MAX_FACT_LEN], (name or md.stem)


def collect() -> tuple[dict[pathlib.Path, list[tuple[str, str]]], list[tuple[str, str]], list[str]]:
    """Returns (repo_facts_by_root, global_facts, unresolved_project_names).

    Each fact is (fact_text, source_relpath_for_provenance).
    """
    cdir = claude_dir()
    repo_facts: dict[pathlib.Path, list[tuple[str, str]]] = {}
    global_facts: list[tuple[str, str]] = []
    unresolved: list[str] = []

    # Global/user native notes: ~/.claude/memory/**
    gmem = cdir / "memory"
    if gmem.is_dir():
        for md in sorted(gmem.rglob("*.md")):
            got = parse_memory_file(md)
            if got:
                global_facts.append((got[0], str(md.relative_to(cdir))))

    # Project-scoped native notes: ~/.claude/projects/<enc>/memory/**
    projects = cdir / "projects"
    if projects.is_dir():
        for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
            mdir = proj / "memory"
            if not mdir.is_dir():
                continue
            facts: list[tuple[str, str]] = []
            for md in sorted(mdir.rglob("*.md")):
                got = parse_memory_file(md)
                if got:
                    facts.append((got[0], str(md.relative_to(proj))))
            if not facts:
                continue
            root = decode_project_dir(proj.name)
            if root and is_git_repo(root):
                repo_facts.setdefault(root, []).extend(facts)
            else:
                unresolved.append(proj.name)
                # stash for optional --unresolved global handling
                repo_facts.setdefault(None, []).extend(facts)  # type: ignore[arg-type]
    return repo_facts, global_facts, unresolved


def _target_global() -> None:
    mem.configure(True)


def _target_repo(root: pathlib.Path) -> None:
    base = root / ".claude"
    base.mkdir(parents=True, exist_ok=True)
    mem.IS_GLOBAL = False
    mem.ROOT = root
    mem.STORE = base / "memory.store.json"
    mem.ACTIVE = base / "memory.active.md"


def add_facts(scope: str, facts: list[tuple[str, str]]) -> tuple[int, int]:
    """Append facts to the currently-targeted store (dedupe in-memory), then
    rebuild the active file once. Returns (added, deduped)."""
    data = mem.load()
    index = {e["fact"].strip().lower(): e for e in data["entries"]}
    added = deduped = 0
    for fact, source in facts:
        key = fact.strip().lower()
        hit = index.get(key)
        if hit:
            hit["votes"] = hit.get("votes", 1) + 1
            hit["last_validated"] = mem.today()
            deduped += 1
            continue
        eid = "m-" + hashlib.sha1((fact + mem.today()).encode()).hexdigest()[:6]
        entry = {
            "id": eid,
            "fact": fact,
            "scope": scope,
            "votes": 1,
            "added": mem.today(),
            "last_validated": mem.today(),
            "access_count": 0,
            "status": "active",
            "source": source,           # provenance; ignored by BM25/embedding ranking
        }
        data["entries"].append(entry)
        index[key] = entry
        added += 1
    mem.save(data)
    mem.cmd_build(argparse.Namespace())   # single rebuild of memory.active.md
    return added, deduped


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write to the stores (default: dry-run preview only)")
    ap.add_argument("--unresolved", choices=["skip", "global"], default="skip",
                    help="project notes whose repo isn't found locally: skip (default) "
                         "or import as global user facts")
    args = ap.parse_args()

    repo_facts, global_facts, unresolved = collect()
    stray = repo_facts.pop(None, [])  # type: ignore[arg-type]
    if args.unresolved == "global":
        global_facts.extend(stray)

    # ---- Preview ----
    total_repo = sum(len(v) for v in repo_facts.values())
    print(f"Source: {claude_dir()}")
    print(f"  global/user notes : {len(global_facts)} fact(s)")
    print(f"  resolved repos    : {len(repo_facts)} ({total_repo} fact(s))")
    for root, facts in repo_facts.items():
        print(f"      {root}  <- {len(facts)} fact(s)")
    if unresolved:
        action = "-> global" if args.unresolved == "global" else "SKIPPED"
        print(f"  unresolved repos  : {len(unresolved)} ({len(stray)} fact(s)) [{action}]")
        for n in unresolved:
            print(f"      {n}")

    if not args.apply:
        print("\nDRY-RUN: nothing written. Re-run with --apply to commit.")
        return

    # ---- Apply ----
    g_added = g_dedup = r_added = r_dedup = 0
    if global_facts:
        _target_global()
        a, d = add_facts("user", global_facts)
        g_added, g_dedup = a, d
    for root, facts in repo_facts.items():
        _target_repo(root)
        a, d = add_facts("repo", facts)
        r_added += a
        r_dedup += d
    print(f"\nApplied. global: +{g_added} new / {g_dedup} deduped | "
          f"repo: +{r_added} new / {r_dedup} deduped")
    print("Active files rebuilt. Embeddings not required; install fastembed only "
          "if the merged store grows past ~200 facts.")


if __name__ == "__main__":
    main()
