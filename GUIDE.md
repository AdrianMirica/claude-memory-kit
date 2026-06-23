# Dynamic Memory for Claude Code — Complete Guide

A persistent, self-maintaining memory layer for tools whose only memory primitive
is "load a markdown file into context" (Claude Code, via `CLAUDE.md`). It
reproduces the core of GitHub Copilot Memory — **scoped, citation-validated,
self-pruning memory that updates automatically from your conversations** — without
any backend, database, or paid service. Pure Python standard library.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Mental model](#2-mental-model)
3. [Architecture](#3-architecture)
4. [The two tiers: global vs repo](#4-the-two-tiers-global-vs-repo)
5. [How memory is captured](#5-how-memory-is-captured)
6. [How memory is validated, decayed, and pruned](#6-how-memory-is-validated-decayed-and-pruned)
7. [The hooks: SessionStart and SessionEnd](#7-the-hooks-sessionstart-and-sessionend)
8. [Install — automatic](#8-install--automatic)
9. [Install — manual](#9-install--manual)
10. [Everyday usage](#10-everyday-usage)
11. [Configuration and customization](#11-configuration-and-customization)
12. [Verifying it works](#12-verifying-it-works)
13. [Troubleshooting](#13-troubleshooting)
14. [Uninstall](#14-uninstall)
15. [Limitations](#15-limitations)
16. [File reference](#16-file-reference)

---

## 1. What it does

- **Remembers across sessions.** Things you tell Claude in one session are
  available in the next — automatically, without you re-explaining.
- **Captures on its own.** You just talk. Explicit "remember…" is saved live
  during the chat; decisions, tech-stack choices, and bug→fix knowledge are
  swept from the transcript when the session ends.
- **Doesn't rot.** Facts tied to code are re-validated against the current
  source; if the cited code changes, the fact is withheld until re-confirmed.
  Stale and low-value facts decay out automatically.
- **Two scopes.** Personal preferences follow you across every repo; project
  facts stay with the project (and can be committed for your team).

---

## 2. Mental model

The host tool stays dumb — it only imports one markdown file. All the "dynamic"
behavior lives in two small Python scripts and a structured store:

```
You talk to Claude
   │
   ├── live:        CLAUDE.md policy → Claude runs `memory.py add` on "remember…"
   └── session end: SessionEnd hook → extract.py sweeps the transcript
   │
   ▼
memory.store.json   ← source of truth (structured; never loaded by Claude directly)
   │  build: validate citations · apply decay · prune
   ▼
memory.active.md    ← GENERATED cache; the ONLY thing Claude loads
   │  @import in CLAUDE.md
   ▼
Next session's context
```

**Key idea:** `memory.store.json` is the database; `memory.active.md` is a
**cache that gets invalidated when the cited code drifts.** That invalidation is
what separates this from a `CLAUDE.md` that silently goes stale.

---

## 3. Architecture

| Component | Role |
| :-- | :-- |
| `scripts/memory.py` | Store engine: `add`, `vote`, `gc`, `build`. Validates citations, applies decay, writes the always-on **core** active file (top-N by score). `--global` selects the cross-session tier. |
| `scripts/retrieve.py` | `UserPromptSubmit` hook. Ranks the whole store against each prompt with BM25 and injects only the top-K relevant facts — so context cost stays constant as the store grows. |
| `scripts/extract.py` | `SessionEnd` hook. Reads the finished transcript and extracts durable facts via two engines (`regex`, `llm`), routing each to the right tier. |
| `scripts/memstore.py` | Shared helpers: path resolution, store IO, tokenization, BM25, core-relevance score. |
| `templates/CLAUDE.global.md` | The capture-policy text + `@memory.active.md` import. Rendered per-machine by the installer. |
| `install.py` | One-shot, OS-agnostic installer. Bakes this machine's Python + paths into Claude Code's global config; idempotent and non-destructive. |
| `memory.store.json` | Source of truth (one per tier). Grows unbounded — load cost stays flat. |
| `memory.active.md` | Generated always-on core Claude loads at session start (one per tier). |

Everything is Python standard library — no `pip install`, no external services.

---

## 4. The two tiers: global vs repo

| | **Global tier** | **Repo tier** |
| :-- | :-- | :-- |
| Lives in | `~/.claude/` (or `$CLAUDE_CONFIG_DIR`) | `<repo>/.claude/` |
| Loaded by | `~/.claude/CLAUDE.md` (every session, every repo) | `<repo>/CLAUDE.md` (only in that repo) |
| Holds | your personal preferences & habits | facts about that project: stack, flows, conventions, bug→fix |
| Validation | quote + votes + 90-day TTL | the above **plus** code-citation hashing |
| Shareable | no — personal to you | yes — commit `.claude/` to the repo |
| CLI selector | `memory.py --global …` | `memory.py …` (default; resolves the current git repo) |

**They link in two places:**

- **At capture time** — one SessionEnd hook (installed globally) reads the
  session's working directory and routes each fact: `user` scope → global store,
  `repo` scope → that repo's store.
- **At load time** — Claude Code loads `CLAUDE.md` hierarchically (global then
  repo) and merges both into context. So inside any project you get *your
  preferences + that project's knowledge* together, with no cross-contamination.

---

## 5. How memory is captured

There are two complementary capture paths. Use both (the default).

### a) Live capture (during the conversation)

`~/.claude/CLAUDE.md` contains a policy instructing Claude: when you reveal a
durable preference ("remember…", "from now on…", "always…", "I prefer…", or a
generalizing correction), **run the memory command immediately**. Claude has a
shell tool, so it executes:

```
<python> "<kit>/scripts/memory.py" --global add "<fact>" --quote "<your words>"
```

This is instant and free (folded into the turn). It reliably catches **explicit**
asks. It is LLM-judgment-driven, so it can miss things — that's what path (b) is for.

### b) Session-end sweep (the safety net)

When a session ends, the `SessionEnd` hook runs `extract.py` over the **finished
transcript**. Two engines:

| Engine | Cost | Catches | Notes |
| :-- | :-- | :-- | :-- |
| `regex` | free, no Claude call | explicit phrases ("remember…", "always…", "I prefer…") → global | deterministic, zero recursion risk |
| `llm` | one isolated `claude -p` call | semantic facts: tech-stack choices, architecture/flow decisions, issue→root-cause→fix, conventions → repo or global | high recall; guarded against recursion |

The `llm` engine spawns a **one-shot headless `claude -p`** — it runs, prints a
JSON list of facts, and exits. It is **not** a resumable conversation and never
appears in your session list. A `MEMORY_HOOK=1` environment guard ensures that
this headless call cannot re-trigger the SessionEnd hook (no infinite loop).

> Extraction happens at SessionEnd (not SessionStart) because it needs the
> *completed* conversation. The SessionStart hook does a different job — see §7.

---

## 6. How memory is validated, decayed, and pruned

Each `build` re-evaluates every entry and writes only the survivors to
`memory.active.md`. Status transitions:

| Condition | Status | Effect |
| :-- | :-- | :-- |
| Repo fact, cited lines unchanged (hash matches) | `active` | included |
| Repo fact, cited file/lines **gone** | `stale` | withheld |
| Repo fact, cited lines **changed** (hash differs) | `needs_revalidation` | withheld until re-confirmed |
| Any fact older than `TTL_DAYS` (default 90) since last validation | `needs_revalidation` | withheld |
| Cold + never-reinforced: unused for `IDLE_DAYS` (default 120) and `votes <= 1` | `graveyard` | auto-pruned (A2) |
| `votes <= 0` | `graveyard` | dropped permanently (kept in store for audit) |

Each retrieved fact's `access_count` / `last_used` is bumped by the retriever, so
frequently useful facts rise in the core ranking and stay warm against idle decay.

**Two selection layers (so the store can grow without bloating context):**
- **Always-on core** — `build` emits only the top `CORE_MAX` (default 20) per tier
  by `score = votes + recency + access`, written to `memory.active.md`.
- **On-demand** — everything else stays in the store and is surfaced per prompt by
  the retriever (§7). Result: store size is unbounded; context cost is constant.

**Citation anchoring** uses a SHA-1 (first 10 hex chars) of the exact cited line
range — dependency-free and language-agnostic. Re-running `add` on the same fact
**votes it up** instead of duplicating. `add` and `vote` auto-run `build`, so the
active file is always current after a capture.

To re-bless a `needs_revalidation` repo fact after intentionally changing code,
re-add it (re-anchors the hash) or update its `last_validated` date in the store
and run `build`.

---

## 7. The hooks: SessionStart and SessionEnd

| Hook | Runs | Why here |
| :-- | :-- | :-- |
| `SessionStart` | `memory.py --global build` | Refresh the always-on core (decay/TTL/validation) so the session opens with current memory. |
| `UserPromptSubmit` | `retrieve.py` | Rank the whole store against the prompt (BM25) and inject the top-K relevant facts for that turn — constant context cost regardless of store size. |
| `SessionEnd` | `extract.py regex llm` | Extract facts from the just-finished transcript. |

**How retrieval keeps context flat.** `UserPromptSubmit` fires before Claude
processes each prompt and receives `{ "prompt", "cwd", ... }` on stdin; whatever
the hook prints to **stdout is injected as context for that turn only**.
`retrieve.py` loads the global store (+ the current repo's store), scores every
non-graveyard entry with BM25 against the prompt (stopword-filtered, with a small
repo/vote boost), and emits at most `TOP_K` (default 6) above a relevance floor.
So a 50-fact store and a 50,000-fact store cost the same at load — only the small
core is static; everything else is fetched on demand. The retriever also honors
the `MEMORY_HOOK=1` guard so it never runs inside the headless extraction call.

**How Claude Code hooks work (relevant facts):**

- Hooks are shell commands declared in `settings.json` as
  *event → matcher group → handlers*.
- `SessionEnd` fires once when a session terminates and receives JSON on **stdin**:
  `{ "session_id", "transcript_path", "cwd", "hook_event_name", "reason" }`.
  `extract.py` reads `transcript_path` and `cwd` from it.
- `SessionEnd` output is ignored (it can't block — the session is already
  closing). Perfect for a fire-and-forget memory append.
- Hooks in `~/.claude/settings.json` apply to **all** projects; hooks in
  `<repo>/.claude/settings.json` apply to that project only.
- On Windows, the installer uses **exec form** (`command` + `args`) with the
  absolute Python path, so there's no shell-quoting ambiguity.

---

## 8. Install — automatic

Run once per machine (works on Windows, macOS, Linux):

```bash
# from the kit directory
python install.py        # Windows
python3 install.py       # macOS / Linux
```

What it does (all idempotent and non-destructive):

1. **Renders** `templates/CLAUDE.global.md` into `<config>/CLAUDE.md`, wrapped in
   managed markers. If the file exists, it **appends** the block (or replaces the
   previous managed block on re-run); your existing content is never touched.
2. **Merges** `<config>/settings.json`:
   - adds the permission rule `Bash(*memory.py*)` so the command runs without prompts
   - adds the `SessionStart` and `SessionEnd` hooks
   - preserves all your other settings; replaces only its own hook entries
3. **Runs an initial build** so `memory.active.md` exists.

`<config>` = `$CLAUDE_CONFIG_DIR` if set, else `~/.claude`. (Tests can override
with `CLAUDE_HOME`.)

After install, **restart Claude Code** and just talk. To verify, see §12.

> The installer bakes in **this machine's** interpreter via `sys.executable`, so
> `python` vs `python3` and the absolute kit path are resolved correctly per OS.
> If you move the kit or change your Python install, just re-run `install.py`.

---

## 9. Install — manual

If you prefer to wire it by hand (or the installer can't run), do the following.
Replace `<PYTHON>` with the absolute path to your interpreter (run
`python -c "import sys; print(sys.executable)"`) and `<KIT>` with the absolute
path to this kit.

### 9.1 Add the import + capture policy to `~/.claude/CLAUDE.md`

Append this (create the file if it doesn't exist). Keep any existing content above it:

```markdown
@memory.active.md

## Automatic memory capture
When I reveal a durable preference ("remember…", "from now on…", "always…",
"I prefer…") or a lasting project fact, immediately run without asking:
  <PYTHON> "<KIT>/scripts/memory.py" --global add "<fact>" --quote "<my words>"
For a project fact (stack, flow decision, issue→fix, convention) use:
  <PYTHON> "<KIT>/scripts/memory.py" add "<fact>" --scope repo --quote "<evidence>"
Do not store secrets, PII, or one-off task instructions. One fact per call.
```

> The `@memory.active.md` import is resolved relative to the file it's written in.
> Because this lives in `~/.claude/CLAUDE.md`, the active file must be at
> `~/.claude/memory.active.md` — which is exactly where `memory.py --global`
> writes it.

### 9.2 Add hooks + permission to `~/.claude/settings.json`

Merge this into your existing `settings.json` (create it if absent):

```json
{
  "permissions": {
    "allow": ["Bash(*memory.py*)"]
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "<PYTHON>",
                     "args": ["<KIT>/scripts/memory.py", "--global", "build"] } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "<PYTHON>",
                     "args": ["<KIT>/scripts/extract.py", "regex", "llm"] } ] }
    ]
  }
}
```

To run **without** the extra `claude -p` call, change the SessionEnd args to
`["<KIT>/scripts/extract.py", "regex"]`.

### 9.3 Create the initial active file

```bash
<PYTHON> "<KIT>/scripts/memory.py" --global build
```

### 9.4 (Optional) Enable the repo tier in a project

In a repo where you want project-scoped memory, add to `<repo>/CLAUDE.md`:

```markdown
@.claude/memory.active.md
```

Project facts are written by `memory.py add --scope repo` (run from inside the
repo) and validated against that repo's code. Commit `<repo>/.claude/` to share
with your team.

---

## 10. Everyday usage

You normally never call these — Claude and the hooks do. But they're available
for manual control and inspection.

```bash
# capture a global preference (cross-session, all repos)
python scripts/memory.py --global add "I prefer short answers" --quote "keep it short"

# capture a repo fact (run inside the repo; validated against its code)
python scripts/memory.py add "Uses Postgres via SQLAlchemy" --scope repo --quote "we use Postgres"
python scripts/memory.py add "Auth lives in src/auth" --scope repo --cite src/auth/jwt.py:1-40

# reinforce or weaken (votes <= 0 removes it)
python scripts/memory.py --global vote m-abc123 +
python scripts/memory.py --global vote m-abc123 -

# rebuild the active file (auto-run by add/vote and the hooks)
python scripts/memory.py --global build   # global tier
python scripts/memory.py build            # repo tier (from inside the repo)

# move stale / dead entries to the graveyard
python scripts/memory.py --global gc
```

To **inspect** what's remembered, open the store or the active file:

- Global store: `~/.claude/memory.store.json`
- Global active (what Claude sees): `~/.claude/memory.active.md`
- Repo store: `<repo>/.claude/memory.store.json`

To **edit or delete** a memory, edit the store JSON directly and run `build`
(set `"status": "graveyard"` to drop one, or change its `votes`).

---

## 11. Configuration and customization

| What | Where | How |
| :-- | :-- | :-- |
| TTL before a fact needs re-validation | `scripts/memory.py` / `memstore.py` → `TTL_DAYS` | default 90 |
| Idle-decay window | `memstore.py` → `IDLE_DAYS` | default 120 |
| Always-on core size | `memory.py` / `memstore.py` → `CORE_MAX` | default 20 per tier |
| Facts injected per prompt | `scripts/retrieve.py` → `TOP_K` | default 6 |
| Retrieval engine | env `MEMORY_ENGINE` | `auto` (default), `bm25`, or `embeddings` |
| When embeddings auto-activate | env `MEMORY_EMBED_THRESHOLD` | default 200 candidates |
| Regex trigger phrases | `scripts/extract.py` → `TRIGGERS` | add patterns |
| LLM extraction instructions | `scripts/extract.py` → `LLM_PROMPT` | tune what's captured |
| Which engines run at session end | `settings.json` SessionEnd `args` | `["…/extract.py","regex"]` drops the Claude call; build auto-nudges to re-enable `llm` if `claude` CLI is present but only `regex` is configured |
| Capture policy wording | `templates/CLAUDE.global.md` | re-run `install.py` to re-render |
| Config dir location | env `CLAUDE_CONFIG_DIR` | both Claude Code and this kit honor it |

### Adaptive retrieval (BM25 → embeddings)

The retriever picks its ranking engine by store size:

- **Small store** → **BM25** (lexical, stdlib, zero dependency, instant).
- **Large store** (≥ `MEMORY_EMBED_THRESHOLD` candidates) → **embeddings**
  (semantic) **if a local backend is installed**, else it stays on BM25.

To enable semantic retrieval, install a local, API-key-free embedding backend:

```bash
pip install fastembed
```

Then it activates automatically once the store crosses the threshold — no config
change. Fact vectors are cached in `<config>/memory.vectors.json` and only
re-embedded when a fact's text changes (the prompt is embedded once per call).
Force a specific engine with `MEMORY_ENGINE=bm25|embeddings`, or lower
`MEMORY_EMBED_THRESHOLD` to try embeddings sooner. If `fastembed` isn't installed,
everything falls back to BM25 silently.

**You don't have to watch for the threshold.** When the store grows past it and no
backend is installed, `build` (which runs at every SessionStart) emits a one-time
`[memory-setup]` hint that Claude relays to you, suggesting `pip install fastembed`.
It shows once per store, never nags, and disappears once the backend is present.

After editing `templates/CLAUDE.global.md`, re-run `install.py` to push changes
into `~/.claude/CLAUDE.md` (only the managed block updates).

---

## 12. Verifying it works

1. **Active file exists and imports correctly:**
   ```bash
   cat ~/.claude/memory.active.md          # macOS/Linux
   Get-Content $HOME\.claude\memory.active.md   # Windows
   ```
2. **Live capture:** start Claude Code, say *"remember that my main branch is
   staging"*. Claude should run the add command (you'll see it) and confirm.
3. **Cross-session recall:** close the session, start a new one, ask *"what's my
   main branch?"* — it should know.
4. **Session-end sweep:** have a short chat that states a preference without the
   word "remember", end the session, then check `memory.store.json` grew.
5. **Hook is registered:**
   ```bash
   python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['hooks'].keys())"
   ```

---

## 13. Troubleshooting

### Nothing is captured automatically
- Make sure you **restarted Claude Code** after install (hooks/CLAUDE.md load at start).
- Say "**remember** …" explicitly — it's the most reliable live trigger.
- Confirm `~/.claude/CLAUDE.md` contains the managed block (`grep "AUTOMATIC MEMORY"`).
- Confirm the permission rule `Bash(*memory.py*)` is in `settings.json`, otherwise
  Claude may be silently blocked from running the command.

### The SessionEnd hook doesn't seem to run
- Validate `settings.json` is valid JSON (`python -m json.tool ~/.claude/settings.json`).
- Check the hook `command`/`args` are **absolute paths** and the Python path
  exists. Re-run `install.py` to regenerate them for this machine.
- Ensure your Claude Code version supports the `SessionEnd` event (update if old).
- Try ending the session normally (e.g. `/clear` or exit) — some abrupt kills may
  skip hooks.

### `claude: command not found` (llm engine)
- The `llm` engine needs the `claude` CLI on `PATH`. If it's not found, the engine
  **skips gracefully** and logs to stderr — capture still works via `regex` and the
  live policy. Either install/`PATH` the `claude` CLI, or switch SessionEnd args to
  `["…/extract.py","regex"]`.

### Claude asks permission every time it saves
- Add/confirm `"Bash(*memory.py*)"` under `permissions.allow` in `settings.json`.
- If your Claude runs commands via a different shell wrapper, broaden the rule or
  approve once and choose "always allow".

### A repo fact never shows up (always withheld)
- Its cited code changed → status is `needs_revalidation`. Re-run
  `memory.py add` with the same fact and a correct `--cite` to re-anchor, or fix
  the `cite` range in the store and `build`.
- Or the cited file/path is wrong → status `stale`. Fix the path and `build`.
- Run `python scripts/memory.py build` and read the `withheld [...]` lines — they
  tell you exactly why each entry was excluded.

### Infinite loop / repeated extraction
- Should be impossible: the `llm` engine sets `MEMORY_HOOK=1` and `extract.py`
  exits immediately if it sees that variable. If you customized the scripts,
  ensure that guard is intact.

### `re.error: bad escape \U` or path errors on Windows
- This was a backslash-in-replacement bug, already fixed (the installer inserts
  the managed block literally). If you hand-edit `install.py`, avoid passing
  Windows paths as a regex replacement string.

### Memory doesn't load in Claude at all
- The `@import` path must resolve relative to the file containing it. Global
  `CLAUDE.md` lives in `~/.claude/`, so it must import `@memory.active.md`
  (same folder) — which is where `--global build` writes it. Repo `CLAUDE.md`
  lives at the repo root, so it imports `@.claude/memory.active.md`.

### `settings.json` got mangled
- The installer backs up invalid JSON to `settings.json.bak` before rewriting.
  Restore from there or from version control, then re-run `install.py`.

### Duplicate-looking facts
- Dedup is by **exact** fact text (case-insensitive). Near-duplicates with
  different wording won't merge automatically — edit the store and `gc` to tidy.

### The active file is getting large / context bloat
- It shouldn't: `build` caps the active file to the top `CORE_MAX` (default 20)
  per tier; everything else is fetched per-prompt by the retriever. If it's still
  large, lower `CORE_MAX` in `scripts/memory.py` / `memstore.py`, down-vote noise,
  and lower `TTL_DAYS` / `IDLE_DAYS`.

### The retriever isn't surfacing a fact I expect
- It's lexical (BM25). If your prompt shares no meaningful (non-stopword) terms
  with the fact, it won't rank. Phrase the prompt with overlapping words, up-vote
  the fact (boosts ranking), or raise `TOP_K` / lower `MIN_SCORE` in `retrieve.py`.
  For semantic recall, swap the BM25 step for embeddings (see §15).

### Retrieval adds latency / I want zero per-prompt cost
- Remove the `UserPromptSubmit` hook from `settings.json`. You lose on-demand
  recall but keep the always-on core + live capture + session-end sweep.

---

## 14. Uninstall

1. Remove the managed block from `~/.claude/CLAUDE.md` (everything between the
   `BEGIN claude-memory-kit` / `END claude-memory-kit` markers).
2. Delete the `SessionStart`, `UserPromptSubmit`, and `SessionEnd` hook entries
   and the `Bash(*memory.py*)` allow rule from `~/.claude/settings.json`.
3. Optionally delete `~/.claude/memory.store.json`, `~/.claude/memory.active.md`,
   `~/.claude/memory.vectors.json`, and any `<repo>/.claude/memory.*` files.

Nothing else is installed — no services, no global packages.

---

## 15. Limitations

- **Relevance ranking is adaptive.** BM25 (lexical) by default; auto-upgrades to
  semantic embeddings once the store is large *and* `fastembed` is installed. While
  on BM25, a paraphrased prompt with no shared terms can miss a fact — install
  `fastembed` (or set `MEMORY_ENGINE=embeddings`) for semantic recall.
- **Retrieval validates at build, not at-use-time.** Repo citations are re-hashed
  at `build` (session start / on capture), not the instant a fact is retrieved.
  Re-build on branch switch for accuracy.
- **LLM-driven capture is heuristic.** Both the live policy and the `llm` engine
  rely on model judgment; they won't catch 100%. Explicit "remember…" is the
  reliable path.
- **Per-prompt hook cost.** `UserPromptSubmit` spawns a short Python process each
  prompt (negligible for BM25). Remove the hook if you want zero per-prompt cost.
- **Hard rules belong in `CLAUDE.md` body, not memory.** Memory is best-effort
  context, not a guaranteed contract. Put non-negotiable rules in the instruction
  text itself.

---

## 16. File reference

```
claude-memory-kit/
├── install.py                    one-shot OS-agnostic installer (idempotent, non-destructive)
├── GUIDE.md                      this document
├── README.md                     quick overview
├── templates/
│   └── CLAUDE.global.md          capture-policy template ({{PYTHON}}/{{MEMORY}} placeholders)
└── scripts/
    ├── memory.py                 store engine: add · vote · gc · build (--global selects tier)
    ├── retrieve.py               UserPromptSubmit hook: adaptive BM25/embedding retrieval (O(K))
    ├── extract.py                SessionEnd hook: regex + llm engines, cwd-routed, guarded
    └── memstore.py               shared helpers: paths, IO, tokenize, BM25, embeddings, scoring
```

Stores and active files are created at install/use time, not shipped:
`<config>/memory.{store.json,active.md,vectors.json}` (global) and
`<repo>/.claude/memory.{store,active}` (per repo).
