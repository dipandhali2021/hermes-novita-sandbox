# novita-sandbox

Adds **Novita Agent Sandbox** as a terminal backend for Hermes, alongside
`local`, `docker`, `singularity`, `modal`, `daytona`, and `ssh`. With
`terminal.backend: novita`, Hermes' `terminal`, `read_file`, `write_file`,
`patch`, `search_files`, and `execute_code` all run inside a Novita sandbox.

## Why it is shaped like this

This plugin **modifies no Hermes source file**, on purpose.

The target install is a shallow, detached snapshot of `NousResearch/hermes-agent`,
and `hermes update` runs `git fetch --depth 1` → **`git reset --hard`** →
`uv pip install -e .`. Any edit to a tracked core file is therefore *silently
reverted* on the next update (and `git clean -fd` removes untracked files inside
the tree as well). Shipping a backend the way Daytona ships one would mean ~40
edit sites across ~25 files — all of which an update erases.

So the plugin lives entirely in `~/.hermes/plugins/`, outside the git worktree,
where `git reset --hard` cannot reach it, and routes `backend: novita` at
runtime. If a future Hermes renames or removes an internal it depends on, one
probe fails, the backend disables itself with a clear log line, and **Hermes
keeps working** — worst case your terminal falls back to the backend you already
had. That is "degrades safely", which is the honest promise; no plugin that
reaches into internals can promise "never breaks".

Design detail and the evidence behind every behavioural claim:
`docs/2026-09-23-novita-sandbox-backend-design.md`, section 12.

## Install

```bash
# 1. the SDK
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python 'novita-sandbox>=2.1.0,<3'

# 2. enable the plugin (user plugins are opt-in)
hermes plugins enable novita-sandbox

# 3. configure it interactively, then activate
hermes novita-sandbox setup
```

`setup` walks through: SDK presence → API key (masked prompt, validated against
the live API with a throwaway sandbox) → template choice → backend selection →
an end-to-end verification through `NovitaEnvironment`.

Non-interactive:

```bash
hermes novita-sandbox setup --api-key sk_... --yes
```

## Usage

```bash
hermes novita-sandbox doctor             # probe report + config + checks
hermes novita-sandbox doctor --live      # also create a real sandbox
hermes novita-sandbox status             # sandboxes on your account
hermes novita-sandbox install-template   # build a custom cpu/memory template

# add Novita to `hermes setup`'s terminal backend menu (see below)
hermes novita-sandbox patch-setup
hermes novita-sandbox unpatch-setup

# activate / revert
hermes config set terminal.backend novita
hermes config set terminal.backend local
```

## The `hermes setup` menu row (read this)

Selecting **Novita** from `hermes setup` → *Terminal Backend* requires editing
`hermes_cli/setup.py`. That is not a shortcut — Hermes offers **no extension
point** for it: the menu is built from function-local literals inside
`setup_terminal_backend()`, and none of the 20 lifecycle hooks fires during
setup. (Platform plugins do get a `setup_fn`, but that path is for gateway
adapters, not terminal backends.)

So `patch-setup` makes the edit in the most conservative way available:

- **two insertions only** — a menu entry and a handler arm;
- **pure additions** — no upstream line is modified or reordered;
- anchored on existing lines, and **refused** if an anchor is missing or
  appears more than once, or if the result would not parse;
- idempotent, with a one-time backup (`setup.py.novita-orig`);
- `unpatch-setup` removes the blocks by marker and restores the file byte for
  byte.

Once applied, `hermes setup` shows:

```
Select terminal backend:
   (○) Local - run directly on this machine (default)
   ...
   (○) Singularity/Apptainer - HPC-friendly container
   (○) Novita - Novita Agent Sandbox (cloud)
 → (●) Keep current (local)
```

and choosing it prompts for the API key (masked), installs the SDK, sets
`novita_template`, and adds `novita-sandbox` to `plugins.enabled` so the
selection is not inert.

### An update reverts it — and the plugin puts it back automatically

`hermes update` runs `git reset --hard`, which erases the edit. **The plugin
re-applies it on the next Hermes start, with no command from you.**

Auto-re-application is deliberately narrow:

- it only ever runs if *you* applied the patch in the first place (recorded in
  `.setup-patch.json`), so it never patches a wizard you did not ask it to touch;
- it goes through the same guards as `patch-setup` — unique anchors, `ast`
  validation, and a **semantic precondition check**;
- if any guard fails it changes nothing, logs a warning, and leaves your
  `hermes setup` working.

The semantic check is what makes automatic editing of a core file defensible.
Before writing, it parses `setup.py` and confirms `setup_terminal_backend()`
still references the names the inserted code relies on — `terminal_choices`,
`idx_to_backend`, `backend_to_idx`, `next_idx`, `selected_backend` — and that the
module still provides `print_success` / `prompt_yes_no` / `save_env_value` and
friends. So a future Hermes that **renames a local** or drops a helper is
**refused**, rather than being injected with code that would raise `NameError`
inside `hermes setup`.

Verified against the real `setup.py`: renaming `next_idx`, dropping
`print_warning`, or rewording the anchor all refuse and leave the file
byte-identical.

Turn it off with `terminal.novita_auto_patch_setup: false` in `config.yaml`, or
with `hermes novita-sandbox unpatch-setup` (which also clears the opt-in).

**The backend is unaffected either way** — only the menu row is ever at stake.

Prefer not to touch a core file at all? Skip both `patch-setup` and
`unpatch-setup` and use `hermes novita-sandbox setup`, which does the same job.
Nothing else in the plugin depends on the patch.

## Configuration (`~/.hermes/config.yaml`)

```yaml
terminal:
  backend: novita
  novita_template: base        # or a template id from install-template
  novita_timeout: 3600         # sandbox lifetime in seconds, from creation
  novita_refresh_window: 3600  # keep-alive target, bumped per execute
  container_persistent: true   # pause on teardown instead of kill
```

The secret `NOVITA_API_KEY` belongs in `~/.hermes/.env` (Hermes' secrets-only
file), never in `config.yaml`.

### Sizing is template-baked

Novita's SDK cannot set cpu/memory at sandbox-create time, and runtime mutation
(`hotplug_memory`, `resize`) returns HTTP 500 — verified live. The `base`
template gives 2 vCPU / 512 MB, which is modest for agent work. To get more,
build a template once:

```bash
hermes novita-sandbox install-template --cpu 2 --memory 4096
```

`base` already ships python3.11, node20, git, bash and tar, so a custom template
is only needed for sizing.

### Lifetime

`novita_timeout` is a lifetime measured **from sandbox creation**, not a
duration added by each call. The backend therefore computes
`elapsed = now - started_at` and extends only when that lengthens the current
deadline — a naive `set_timeout(window)` would move a deadline *backwards* on a
long session.

## Behaviour

- **Persistence.** With `container_persistent: true`, sandboxes are created with
  `on_timeout: pause` + `auto_resume`, tagged `hermes_task_id`, and resumed by
  task on the next session. Teardown pauses and syncs files back to your host; it
  does not kill. Set it to `false` to kill on teardown instead.
- **Interrupts pause, not kill.** Ctrl-C during a command pauses the sandbox so
  the filesystem survives; the next command resumes it.
- **File sync.** Your skills tree, skill scripts, cache, and opt-in credential
  files are uploaded to the sandbox (batched, ~550 files / ~22 MB for a typical
  home), and remote edits are pulled back on teardown. Credential files are
  opt-in via Hermes' own config — the plugin adds no new file exposure.
- **A dropped connection is retried once** after a reconnect rather than failing
  your command.

## Known differences from a core backend

Both are consequences of not editing core, and both are safe:

1. **Commands still go through Hermes' approval prompts.** Core's
   sandboxed-backend approval bypass uses inline literals the plugin cannot
   reach. More friction, not less safety.
2. **Per-task resource overrides do not apply.** Global `terminal.*` config is
   read instead.

Also cosmetic: `hermes setup`'s backend menu, `hermes doctor`, `hermes status`
and the dashboard dropdown do not list `novita`. Use
`hermes novita-sandbox doctor` for the diagnostics those would provide.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `terminal` says the tool is unavailable | `hermes novita-sandbox doctor` — a pivotal probe failed, or `NOVITA_API_KEY` is unset |
| `NOVITA_API_KEY is not set` | run `hermes novita-sandbox setup` |
| `novita-sandbox is not installed` | see Install step 1 |
| terminal calls blocked with a "backend unavailable" message | a Hermes update changed an internal; `doctor` names the drifted probe |
| Commands run locally instead of in Novita | `terminal.backend` is not `novita` |
| `Unknown TERMINAL_ENV: novita` | the plugin is not enabled: `hermes plugins enable novita-sandbox`, then restart |

## Layout

| File | Role |
|---|---|
| `environment.py` | `NovitaEnvironment(BaseEnvironment)` — no Hermes internals, no patching |
| `inject.py` | runtime routing: 7 capability-probed patches + unavailability guard |
| `config.py` | `terminal.*` + `NOVITA_API_KEY` resolution |
| `cli.py` | `hermes novita-sandbox setup\|doctor\|status\|install-template` |
| `probes/` | the live API probes that established the SDK's real behaviour |
| `tests/` | 65 offline tests; the inject suite simulates internals changing |

## Tests

```bash
uv run --no-project --python ~/.hermes/hermes-agent/venv/bin/python \
       --with pytest pytest tests/ -q
```

`tests/test_inject.py` is the important one: it drives the probes against
synthetic modules whose internals have been renamed, removed, or retyped, and
asserts that `install()` never raises and never half-wires the backend.
