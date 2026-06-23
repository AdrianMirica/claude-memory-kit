#!/usr/bin/env python3
"""Cross-platform installer for the dynamic-memory layer (Windows/macOS/Linux).

Run once per machine:   python install.py        (or python3 install.py)

It bakes THIS machine's absolute Python interpreter (sys.executable) and script
paths into Claude Code's global config, so the hooks work regardless of OS and
regardless of `python` vs `python3` naming. Re-running is idempotent.

What it does:
  1. Renders templates/CLAUDE.global.md -> <config>/CLAUDE.md  (with @memory.active.md)
  2. Merges <config>/settings.json:
       - permission allow rule so the memory command runs without prompting
       - SessionStart hook -> memory.py --global build   (refresh/decay on open)
       - SessionEnd  hook -> extract.py                  (capture from transcript)
  3. Runs an initial build so memory.active.md exists.

<config> = $CLAUDE_CONFIG_DIR or ~/.claude  (override with CLAUDE_HOME for tests).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys

KIT = pathlib.Path(__file__).resolve().parent
MEMORY = KIT / "scripts" / "memory.py"
EXTRACT = KIT / "scripts" / "extract.py"
RETRIEVE = KIT / "scripts" / "retrieve.py"
TEMPLATE = KIT / "templates" / "CLAUDE.global.md"

# Engines for the SessionEnd extractor. Drop "llm" for zero extra Claude calls.
EXTRACT_ENGINES = ["regex", "llm"]
ALLOW_RULE = "Bash(*memory.py*)"
BLOCK_BEGIN = "<!-- BEGIN claude-memory-kit (managed) — do not edit inside this block -->"
BLOCK_END = "<!-- END claude-memory-kit (managed) -->"


def config_dir() -> pathlib.Path:
    base = pathlib.Path(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or (pathlib.Path.home() / ".claude")
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def render_claude_md(cfg: pathlib.Path) -> None:
    """Create CLAUDE.md if missing, else append our managed block. Idempotent:
    re-running replaces only the marked block and never touches user content."""
    rendered = TEMPLATE.read_text(encoding="utf-8")
    rendered = rendered.replace("{{PYTHON}}", sys.executable).replace("{{MEMORY}}", str(MEMORY))
    block = f"{BLOCK_BEGIN}\n{rendered.rstrip()}\n{BLOCK_END}\n"

    target = cfg / "CLAUDE.md"
    if target.exists():
        content = target.read_text(encoding="utf-8")
        existing = re.compile(
            re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL
        )
        if existing.search(content):
            content = existing.sub(lambda _: block, content)  # replace prior managed block
            action = "updated managed block in"
        else:
            content = content.rstrip("\n") + "\n\n" + block  # append, keep user content
            action = "appended managed block to"
    else:
        content = block
        action = "created"
    target.write_text(content, encoding="utf-8")
    print(f"{action} {target}")


def is_ours(group: dict) -> bool:
    """True if a hook group references our scripts (so re-install replaces it)."""
    blob = json.dumps(group)
    return any(s in blob for s in ("memory.py", "extract.py", "retrieve.py"))


def hook_group(args: list[str]) -> dict:
    return {"hooks": [{"type": "command", "command": sys.executable, "args": args}]}


def merge_settings(cfg: pathlib.Path) -> None:
    path = cfg / "settings.json"
    settings = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"warning: {path} is not valid JSON; backing up and recreating")
            path.rename(path.with_suffix(".json.bak"))

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    if ALLOW_RULE not in allow:
        allow.append(ALLOW_RULE)

    hooks = settings.setdefault("hooks", {})
    for event, args in (
        ("SessionStart", [str(MEMORY), "--global", "build"]),
        ("UserPromptSubmit", [str(RETRIEVE)]),
        ("SessionEnd", [str(EXTRACT), *EXTRACT_ENGINES]),
    ):
        groups = [g for g in hooks.get(event, []) if not is_ours(g)]
        groups.append(hook_group(args))
        hooks[event] = groups

    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"merged {path}")


def initial_build(cfg: pathlib.Path) -> None:
    env = {**os.environ, "CLAUDE_HOME": str(cfg)}
    subprocess.run([sys.executable, str(MEMORY), "--global", "build"], env=env, check=False)


def main() -> None:
    if not TEMPLATE.exists():
        sys.exit(f"template missing: {TEMPLATE}")
    cfg = config_dir()
    print(f"Claude config dir: {cfg}")
    print(f"Python:            {sys.executable}")
    render_claude_md(cfg)
    merge_settings(cfg)
    initial_build(cfg)
    print("\nDone. Restart Claude Code; capture runs automatically from now on.")
    print("To disable the extra Claude call, edit settings.json SessionEnd args -> [\"<extract.py>\", \"regex\"].")


if __name__ == "__main__":
    main()
