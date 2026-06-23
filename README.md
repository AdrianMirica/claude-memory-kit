# Dynamic memory on top of a static markdown tool (Claude Code)

Reproduces the core of GitHub Copilot Memory — **citation-validated, scoped,
self-pruning memory** — for a tool whose only memory primitive is "load a
markdown file into context".

## Two tiers

| Tier | Store | Loaded by | Holds | Survives |
|---|---|---|---|---|
| **Global** (`--global`) | `~/.claude/memory.store.json` | `~/.claude/CLAUDE.md` (every session, every repo) | your preferences | **session switches + all repos** |
| **Repo** (default) | `<repo>/.claude/memory.store.json` | `<repo>/CLAUDE.md` | repo facts (code-cited) | committed in the repo |

The **global tier is the cross-session memory**: Claude reads `~/.claude/CLAUDE.md`
on every session start, so anything captured there is remembered next session.
Global entries are quote/vote/TTL-validated (not tied to a repo). Repo entries
add code-citation validation on top.

## How it works

```
memory.store.json   source of truth (grows unbounded; load cost stays flat)
        │
        ├── build  → memory.active.md   ALWAYS-ON CORE (top-N by score) ── @import in CLAUDE.md ─┐
        │            (validate · decay · prune · cap)                                              │
        │                                                                                          ▼
        └── retrieve (per prompt, BM25)  → top-K relevant facts ── injected via UserPromptSubmit ─▶ Claude
```

Two selection layers keep context flat as the store grows: a small **always-on
core** (loaded statically), plus **on-demand retrieval** that ranks the whole
store against each prompt and injects only the top-K. Context cost is O(K),
independent of store size. The host tool stays dumb — it just imports a file and
accepts the hook's injected context.

### What maps to what (vs. Copilot Memory)

| Copilot Memory          | Here                                             |
|-------------------------|--------------------------------------------------|
| Repo facts vs user prefs| `scope: repo \| user` + hierarchical CLAUDE.md   |
| Citations to code       | `cite: path:start-end` + content-hash anchor     |
| **Validate on use**     | `build` re-hashes cited lines; drift -> withheld |
| Reinforcement / decay   | `votes` + 90-day TTL + idle prune; `<=0` -> graveyard |
| **Relevance ranking**   | adaptive: BM25 (small) → embeddings (large, `UserPromptSubmit`) |
| Cross-session sharing   | the committed store file                         |

The non-negotiable piece is **validation**: a repo fact is only emitted to the
active file if its cited lines still hash to the stored value. That is what
stops the file from rotting into a stale `CLAUDE.md`.

## Install (automatic capture — Windows / macOS / Linux)

```bash
# stdlib only, no pip installs. Run ONCE per machine:
python install.py        # Windows
python3 install.py       # macOS / Linux
```

The installer bakes this machine's interpreter (`sys.executable`) and absolute
paths into Claude Code's global config, so it is OS-agnostic by construction. It:

- renders `<config>/CLAUDE.md` (capture policy + `@memory.active.md` import)
- merges `<config>/settings.json` (idempotent; preserves your other settings):
  - **SessionStart** hook -> `memory.py --global build` (refresh + decay on open)
  - **UserPromptSubmit** hook -> `retrieve.py` (inject top-K relevant facts per prompt)
  - **SessionEnd** hook -> `extract.py` (capture facts from the finished transcript)
- runs an initial build

`<config>` = `$CLAUDE_CONFIG_DIR` or `~/.claude`. After this, just talk to Claude —
explicit "remember…" is captured live; decisions/stack/issue→fix are swept at
session end. To drop the extra Claude call, set the SessionEnd args to
`["<path>/extract.py", "regex"]`.

### Why SessionStart + SessionEnd (not one hook)

- **Extract at SessionEnd** — the conversation must be *finished* to extract from.
- **Build at SessionStart** — so the new session loads the freshest, decayed memory.

The SessionEnd `llm` engine spawns a single isolated `claude -p` one-shot (not a
resumable conversation), guarded against recursion via `MEMORY_HOOK=1`.

## Manual usage (also what Claude runs under the hood)

```bash
# capture (repo tier validates the citation; global tier is cross-session)
python scripts/memory.py add "Auth lives in the JWT module" --scope repo --cite src/auth/jwt.py:1-40
python scripts/memory.py --global add "Concise commits" --quote "keep commits short"

# reinforce / weaken (use an id printed by `add`)
python scripts/memory.py vote m-abc123 +
python scripts/memory.py vote m-abc123 -

# regenerate the file Claude loads (auto-run by add/vote and the hooks)
python scripts/memory.py build            # repo tier
python scripts/memory.py --global build   # global tier
```

## A week of real use (what actually accumulates)

Concrete scenario: you work across two repos — `api-service` and `web-app` — over
five days and many sessions. Here's how memory builds, routes, and decays.

**Mon — `api-service`.** You say *"I prefer short, direct answers"* and *"we use
Postgres via SQLAlchemy here."* Live capture routes the first to **global**
(applies everywhere) and the second to the **repo** store (it's project-specific).
Session ends; the SessionEnd sweep also catches *"the 500s were a missing await in
the cache layer — fixed with a lock"* → repo fact.

```
~/.claude/memory.active.md          → "Prefers short, direct answers"
api-service/.claude/...active.md    → "Uses Postgres via SQLAlchemy",
                                       "Cache race caused 500s; fixed with a lock"
```

**Tue — switch to `web-app`.** New session, different repo. Claude still loads the
**global** prefs (short answers), but **none** of `api-service`'s Postgres/cache
facts leak in — the repo tier is scoped. You add *"web-app is Vite + React, state
via Zustand."* → goes to `web-app`'s repo store only.

**Wed — back in `api-service`, three sessions.** You restate *"keep answers
short"* — dedup **votes it up** (now stronger) instead of duplicating. You refactor
the cache layer; the file backing the *"fixed with a lock"* fact changes. Next
`build` re-hashes the cited lines, sees drift, and **withholds** that fact
(`needs_revalidation`) — it silently drops out of context instead of misleading you.

**Thu — a contradiction.** You tell Claude *"actually, give detailed answers when
reviewing PRs."* The capture policy votes the blunt *"short answers"* global pref
**down** and adds the nuanced one. Memory self-corrects.

**Fri — cumulative state.** You now have a handful of high-signal global prefs that
follow you into every repo, plus two independent project knowledge bases. Sessions
start instantly with the right context; nothing you said on Monday was re-explained.

### What you'd observe after the week

- **Global store:** ~5–10 durable prefs, the useful ones with higher `votes`.
- **Per-repo stores:** each holds only *that* project's stack/flows/bug→fix —
  committable so teammates inherit them.
- **The graveyard:** the down-voted "short answers" pref and the stale cache fact —
  retained for audit, excluded from context.
- **Context stays lean:** because drift-withholding + decay + voting continuously
  prune, the injected `memory.active.md` doesn't balloon over time — the failure
  mode of a hand-maintained `CLAUDE.md`.

### Where it needs a human hand

- Near-duplicate facts with *different wording* won't auto-merge — occasional
  `gc` + a store tidy keeps things crisp.
- A repo fact withheld after a big refactor needs a quick re-add to re-anchor its
  citation (or you accept it decayed away).
- Cross-machine: the global store is per-machine; sync `~/.claude/memory.store.json`
  via dotfiles if you want the same prefs on your laptop and desktop.

## Limitations vs. the real thing

- **Adaptive retrieval** — BM25 (lexical, zero-dep) while the store is small;
  auto-upgrades to semantic embeddings once large, *if* `pip install fastembed`.
  On BM25, a paraphrased prompt with no shared terms can miss a fact.
- **Batch, not at-use-time validation** — facts are validated at `build`, not the
  instant they're retrieved. Re-build on branch switch to mitigate.
- **Heuristic capture** — LLM/regex driven; explicit "remember…" is the reliable path.
- **Per-prompt hook cost** — a short Python spawn per prompt (negligible for BM25;
  remove the `UserPromptSubmit` hook for zero cost).
- **Hard rules belong in `CLAUDE.md` body**, not memory — memory is best-effort.
