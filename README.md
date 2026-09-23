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
[`docs/2026-09-23-novita-sandbox-backend-design.md`](docs/2026-09-23-novita-sandbox-backend-design.md), section 12.

## Features

In plain words:

- **Runs your commands in the cloud instead of on your machine.** Pick `novita`
  like you'd pick `docker` or `daytona`, and every shell command and file
  operation Hermes makes happens inside a Novita sandbox.
- **Your work in the sandbox is not lost.** When you're done it *pauses* rather
  than being deleted, so your next session carries on from where you stopped.
- **Resumes by itself.** A paused sandbox wakes up automatically the next time a
  command needs to run — you don't do anything.
- **Cheap by default.** A sandbox pauses after ~5 idle minutes. Paused sandboxes
  aren't billed for CPU or memory, so idle time costs you nothing.
- **Shows up in `hermes setup`.** One command adds Novita to the normal backend
  menu, where it asks for your API key like the other backends do.
- **Survives Hermes updates.** Updating Hermes wipes changes to its own code.
  This is a plugin in `~/.hermes/plugins/`, which isn't touched — and it puts
  itself back automatically if an update removes anything.
- **Tells you when something breaks.** `doctor` reports what's working. If a
  Hermes update changes something it relies on, it says so loudly instead of
  failing in a confusing way.
- **No cleanup chores.** One command pauses every sandbox you have (or deletes
  them) and shows what's running.
- **Installs nothing into Hermes' own code.** No fork, no patch file to re-apply,
  nothing to rebase.

## Install

```bash
# 1. install the plugin — clones into ~/.hermes/plugins/ and offers to enable it
hermes plugins install dipandhali2021/hermes-novita-sandbox

# 2. configure it — installs the SDK, stores your key, picks a template, verifies
hermes novita-sandbox setup
```

`setup` walks through: SDK presence → API key (masked prompt, validated against
the live API with a throwaway sandbox) → template choice → backend selection →
an end-to-end verification through `NovitaEnvironment`.

To update later:

```bash
hermes plugins update novita-sandbox     # git pull inside the plugin dir
```

Non-interactive:

```bash
hermes novita-sandbox setup --api-key sk_... --yes
```

<details>
<summary>Manual install (without <code>hermes plugins install</code>)</summary>

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python 'novita-sandbox>=2.1.0,<3'
hermes plugins enable novita-sandbox
hermes novita-sandbox setup
```

Note that user plugins are opt-in: a directory under `~/.hermes/plugins/` does
not load unless its key is in `plugins.enabled` in `config.yaml`. The
`hermes plugins install` route handles that for you (it prompts to enable).

</details>

## Usage

```bash
hermes novita-sandbox doctor             # probe report + config + checks
hermes novita-sandbox doctor --sandboxes # ...and the sandboxes on your account
hermes novita-sandbox doctor --live      # also create a real sandbox
hermes novita-sandbox install-template   # build a custom cpu/memory template

# cost control: pause (or delete) every sandbox this plugin created
hermes novita-sandbox stop --all
hermes novita-sandbox stop --all --delete

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

## Cost (read this)

**A running sandbox costs money. A paused one effectively does not.**

Per Novita's published pricing:

| State | Billed for |
|---|---|
| **Running** | vCPU + RAM, **per second**. Billing stops when the sandbox stops. |
| **Paused** | CPU and RAM billing **stops**. Only Persistent Storage is billed — **60 GB free per account**, and a synced `.hermes` tree is ~24 MB. |
| Templates | Free. |

At the `base` template's sizing (2 vCPU / 512 MiB):

```
vCPU   2 × $0.0000098/s  = $0.0000196/s
RAM    0.5 GiB × $0.0000032/s = $0.0000016/s
                     total ≈ $0.0000212/s
                          ≈ $0.0013/min
                          ≈ $0.076/hour
                          ≈ $1.83/day   if left running
```

So the only number that matters for cost is **how long a sandbox stays
*running* after you stop using it.** That is what `novita_timeout` controls,
and it defaults to **300s (5 minutes)** deliberately:

- a *running* sandbox is billed, a *paused* one is not;
- sandboxes are created with `on_timeout: pause` + `auto_resume`, so a paused
  sandbox **resumes transparently** on the next terminal command, filesystem intact.

Pausing is therefore free and costs you nothing but a few seconds on the next
command — so a long timeout buys nothing and bills you for idle time. An
earlier default of 3600s was a mistake on my part: it left an abandoned sandbox
running (and billing) for up to an hour.

### Reclaiming on demand

```bash
hermes novita-sandbox stop --all            # pause everything (stops billing, keeps files)
hermes novita-sandbox stop --task default   # just one task's sandbox
hermes novita-sandbox stop --all --delete   # destroy instead (filesystem is lost)
hermes novita-sandbox doctor --sandboxes    # see what exists, and its task id
```

`stop` connects to each sandbox and pauses it — it does not resume anything, and
it does not touch sandboxes this plugin did not create (it selects only those
carrying the `hermes_task_id` metadata). Sandboxes that are **already paused are
skipped**, because `connect()` *resumes* a paused sandbox before returning — so
touching one to "pause it again" would briefly restart its meter.

### `connect()` resumes — the trap worth knowing

Novita's `sandbox.connect(id)` **resumes a paused sandbox** as a side effect. So
any tool or script that connects to a paused sandbox to *inspect* it will wake it
and restart billing. `sandbox.list()` is safe (it does not connect); `get_info()`
on a connected sandbox is not. If you write your own scripts against these
sandboxes, list for state and avoid connecting unless you intend to run something.

For the same reason the backend creates sandboxes with **`auto_resume: false`**.
Auto-resume sounds convenient, but it means any stray SDK call or HTTP request
wakes a paused sandbox and silently restarts the meter. `_ensure_ready()` resumes
explicitly via `connect()` when Hermes actually needs a command to run, which is
both sufficient and predictable.

### Never persist at all

Set `container_persistent: false` and sandboxes are **killed on teardown**
rather than paused: no storage retention, nothing left behind — at the cost of
losing the sandbox filesystem between sessions.

### Free vs paid tier

Novita's free tier allows 5 concurrent sandboxes and a **1-hour maximum session
length** (2 vCPU / 4 GB). Topping up any balance unlocks the paid tier (100
concurrent, 24-hour sessions, up to 8 vCPU / 8 GB) automatically. Note the
1-hour cap: with `novita_timeout` at 300s you will pause long before it, but a
single *command* should not be expected to run for over an hour on the free tier.

Check actual spend at `novita.ai/sandbox-console/usage`, or Billing → Usage-based
Billing → Agent Sandbox.

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
| `novita-sandbox is not installed` | run `hermes novita-sandbox setup` (installs the SDK) |
| terminal calls blocked with a "backend unavailable" message | a Hermes update changed an internal; `doctor` names the drifted probe |
| Commands run locally instead of in Novita | `terminal.backend` is not `novita` |
| `Unknown TERMINAL_ENV: novita` | the plugin is not enabled: `hermes plugins enable novita-sandbox`, then restart |

## Layout

| File | Role |
|---|---|
| `plugin.yaml` | manifest (`kind: standalone`, `manifest_version: 1`) |
| `after-install.md` | shown by `hermes plugins install` right after installing |
| `environment.py` | `NovitaEnvironment(BaseEnvironment)` — no Hermes internals, no patching |
| `inject.py` | runtime routing: 7 capability-probed patches + unavailability guard |
| `config.py` | `terminal.*` + `NOVITA_API_KEY` resolution |
| `cli.py` | `hermes novita-sandbox setup\|doctor\|install-template\|stop\|patch-setup\|unpatch-setup` |
| `probes/integration_check.py` | live end-to-end check (creates a real sandbox) |
| `tests/` | offline tests; the inject suite simulates Hermes internals changing |

## Tests

```bash
uv run --no-project --python ~/.hermes/hermes-agent/venv/bin/python \
       --with pytest pytest tests/ -q
```

`tests/test_inject.py` is the important one: it drives the probes against
synthetic modules whose internals have been renamed, removed, or retyped, and
asserts that `install()` never raises and never half-wires the backend.
