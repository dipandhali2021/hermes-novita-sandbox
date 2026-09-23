# Novita Sandbox Terminal Backend — Design Spec

**Date:** 2026-09-23
**Status:** Approved (design review), pending spec review
**Target:** Hermes Agent 0.17.0 at `~/.hermes/hermes-agent` (shallow detached snapshot)
**Deliverable:** an update-proof Hermes plugin adding `novita` as a terminal backend

---

## 1. Purpose

Add [Novita Agent Sandbox](https://novita.ai/docs/guides/sandbox-overview) as a terminal
execution backend in Hermes, selectable as `terminal.backend: novita` alongside the
existing `local`, `docker`, `singularity`, `modal`, `daytona`, and `ssh` backends.

### Hard requirement (drives the entire design)

> A future update of Hermes must not break this plugin.

### Why that requirement forbids editing core files

Verified facts about the target install:

| Check | Result |
|---|---|
| `~/.hermes/hermes-agent/.install_method` | `git` |
| `.git/shallow` | **present — shallow clone** |
| `git rev-list --count HEAD` | `1` (a single reachable commit) |
| `git rev-list --count HEAD..origin/main` | **40165** commits behind |
| `git branch -vv` | `HEAD` is **detached**, not on a branch |
| `git remote -v` | `https://github.com/NousResearch/hermes-agent.git` |
| `git status --short` | clean except untracked `.install_method` |

`hermes_cli/main.py:52-53` documents the upgrade path:

> When the user upgrades code via `git pull` (or `hermes update` crashes between
> `git reset --hard` and `uv pip install -e .`) …

So an update is `git fetch --depth 1` → **`git reset --hard`** → `uv pip install -e .`.
Any modification to a tracked core file is therefore **silently reverted**, not merely
merge-conflicted; `git clean -fd` also removes untracked files *inside* the repo tree.

**Consequence:** the only durable location is `~/.hermes/plugins/`, a sibling of the
repo worktree and untouchable by `git reset --hard` / `git clean -fd` run inside it.
The plugin must live there and must not write anything into the Hermes source tree.

### Scale of the alternative (why this is a plugin, not a patch)

For reference, shipping a backend the way `daytona` ships requires ~40 edit sites
across ~25 files, because the backend name is re-tested in an `if/elif` ladder and in
scattered inline name-sets:

- `tools/terminal_tool.py` — factory `:1225-1372`, requirement check `:2612-2620`,
  container-config gate `:2013`, per-backend image select `:1919-1926`,
  cwd sanitisation `:1151`, `container_backend` set `:1096`, `_ISOLATION_KEYS` `:1027`,
  docstrings/error strings `:1235/:2618/:2662/:2667`
- `hermes_cli/config.py` — `DEFAULT_CONFIG["terminal"]` `:1020-1110`,
  `TERMINAL_CONFIG_ENV_MAP` `:5728-5755`, status display `:6667-6681`,
  `OPTIONAL_ENV_VARS` `:2885`
- `tools/lazy_deps.py:161-163`; `hermes_cli/setup.py:1183-1195` + `:1353-1404`;
  `hermes_cli/doctor.py:1458-1478`; `hermes_cli/status.py:413-415`;
  `hermes_cli/web_server.py:514-518`; `hermes_cli/tips.py:150`
- `agent/prompt_builder.py:862,871`; `tools/env_probe.py:52`; `tools/skills_tool.py:109`
- `tools/file_tools.py:799-808`; `tools/code_execution_tool.py:644-652`;
  `tools/approval.py:1229,1480,1797`; `cli.py:403,611`; `gateway/run.py:1472`;
  `batch_runner.py:308`

All of which `git reset --hard` erases on the next update. Hence the plugin approach.

---

## 2. Constraints inherited from Hermes

From `AGENTS.md`:

> plugins MUST NOT modify core files … If a plugin needs a capability the framework
> doesn't expose, expand the generic plugin surface (new hook, new ctx method) — never
> hardcode plugin-specific logic into core.

**Verified: no plugin extension point for execution environments exists today.**
Full `ctx.register_*` surface in `hermes_cli/plugins.py`:
`register_tool :367`, `register_cli_command :437`, `register_command :462`,
`register_context_engine :549`, `register_image_gen_provider :581`,
`register_dashboard_auth_provider :608`, `register_video_gen_provider :648`,
`register_web_search_provider :675`, `register_browser_provider :703`,
`register_tts_provider :735`, `register_transcription_provider :773`,
`register_platform :817`, `register_slack_action_handler :873`,
`register_auxiliary_task :933`, `register_hook :1044`, `register_middleware :1063`,
`register_skill :1084`. None register an environment/terminal backend.

`_VALID_PLUGIN_KINDS` (`plugins.py:257`) = `{standalone, backend, exclusive, platform,
model-provider}`; the `"backend"` kind means *provider* backends (image-gen, web-search,
browser, TTS, STT) selected via `<category>.provider`, **not** terminal backends.

The only plugin seam in the terminal path is the fail-open output hook
`tools/terminal_tool.py:2446-2456` (`transform_terminal_output`) which can rewrite output
text but cannot select a backend. `pre_tool_call` can only **block** a tool
(`{"action": "block", "message": ...}`, see `get_pre_tool_call_block_message`,
`plugins.py:1907`) — it cannot redirect execution. So neither hook can swap the backend.

**Therefore:** the plugin uses the sanctioned `register(ctx)` surface for its CLI command,
and reaches the terminal path by non-destructive runtime injection (Section 6).

---

## 3. Verified Novita API surface

Researched from the official docs and PyPI; **to be re-confirmed by a live probe during
implementation** (Section 12).

- Package: `novita-sandbox`, PyPI **v2.1.1** (v2.1.0 documented), pure Python,
  `requires-python >=3.9`, MIT. Depends on `httpx<1,>=0.27`, `protobuf`, `pydantic>=2.12.4,<3`,
  `rich>=14`, `dockerfile-parse`, `wcmatch`, `attrs`, `typing-extensions`, `packaging`,
  `python-dateutil`. The SDK is E2B-derived; the API mirrors E2B closely.
- Client: `from novita_sandbox import Novita`. Legacy path
  `from novita_sandbox.code_interpreter import Sandbox` also exists.
- Create: `novita.sandbox.create(template, timeout=<seconds>, envs={...}, metadata={...},
  on_timeout="pause"|"kill", auto_resume=bool, secure=bool)`.
  Official templates include `base`, `codex`, `claude-code`, `desktop`.
- **Default lifetime is 5 minutes** (docs: "By default, a new sandbox stays alive for
  5 minutes"). Python `timeout` is in **seconds** (JS uses `timeoutMs`).
- Reconnect: `novita.sandbox.connect(sandbox_id, timeout=<seconds>)` — connects to a
  running or paused sandbox and resumes a paused one. For a running sandbox the timeout is
  updated only if the new value is longer.
- List: `novita.sandbox.list(query=SandboxQuery(state=[SandboxState.RUNNING, ...]),
  metadata={...})` → paginator with `.next_items()` / `.has_next`.
- Exec: `sandbox.commands.run(cmd, timeout=<s>, cwd=..., envs=...,
  background=False, on_stdout=..., on_stderr=...)` → `CommandResult` with
  `stdout`, `stderr`, `exit_code`, `error`.
  **`timeout=0` means no timeout.**
  **A non-zero exit raises `CommandExitException`**, which carries the same
  `stdout` / `stderr` / `exit_code` / `error` fields.
- Background: `commands.run(..., background=True)` → `CommandHandle` (`.wait()`,
  and `commands.kill()`).
- Files: `sandbox.files.write(path, <bytes | file-like>)`, `files.read(path)`,
  `files.list(dir)`, and batch `files.write_files([{"path": ..., "data": bytes}, ...])`.
- Lifecycle: `sandbox.kill()`, `sandbox.pause()`, `sandbox.set_timeout(...)`,
  `sandbox.get_info()`, `sandbox.sandbox_id`, `sandbox.get_host(port)`.
- Git: `sandbox.git.clone(url, path=, username=, password=, depth=)`.
- Templates: `Template().from_image("nikolaik/python-nodejs:python3.11-nodejs20")` then
  `Template.build(template, "hermes-novita", cpu_count=2, memory_mb=4096)` →
  `build.template_id`. Also `from_python_image`, `from_ubuntu_image`, `from_node_image`,
  `from_dockerfile`, `from_base_image`, `from_template`; build steps include `run_cmd`,
  `apt_install`, `pip_install`, `npm_install`, `copy`, `set_envs`, `git_clone`.
  `novita.template.list(template_type=..., page=, limit=)` enumerates templates with
  `template_id`, `names`, `cpu_count`, `memory_mb`, `disk_size_mb`.

### The critical correctness trap

`tools/environments/base.py`'s `_ThreadedProcessHandle` requires
`exec_fn() -> (output_str, exit_code)` (`base.py:205-231`), and `DaytonaEnvironment._run_bash`
demonstrates the convention by returning `(response.result or "", response.exit_code)`
(`daytona.py:238-240`).

Novita's SDK **raises** on non-zero exit instead of returning the code. Any implementation
that calls `commands.run` directly will convert every benign non-zero exit — `grep` with no
match, `test -f`, `exit 1` — into a Python exception, breaking Hermes' command layer
(built-in `terminal`, `read_file`, `write_file`, `patch`, `search_files`, `execute_code`).
The `except CommandExitException` unwrap in Section 5 is therefore **mandatory**, and is the
first thing the test suite must cover.

---

## 4. Architecture — two layers

```
~/.hermes/plugins/novita-sandbox/
├── plugin.yaml          manifest
├── __init__.py          register(ctx)   — sanctioned plugin entry point
├── environment.py       Layer 1: NovitaEnvironment(BaseEnvironment)
├── inject.py            Layer 2: runtime injection + capability probes
├── config.py            terminal.* + NOVITA_API_KEY resolution
├── cli.py               hermes novita-sandbox doctor|install-template
├── tests/               offline unit + probe tests; live tests opt-in
└── docs/
    └── 2026-09-23-novita-sandbox-backend-design.md   ← this file
```

**Layer 1 — `environment.py`.** A plain `BaseEnvironment` subclass, the same shape as
`tools/environments/daytona.py`. Imports only `tools.environments.base`,
`tools.environments.file_sync`, and `novita_sandbox`. Performs **no** monkeypatching and
holds no knowledge of Hermes internals. ~80% of the implementation; fully unit-testable
offline against a mocked SDK.

**Layer 2 — `inject.py`.** Small, defensive adapter that routes Hermes' `novita` backend
selection to Layer 1, plus the capability probes that decide whether injection is possible.

**Rationale.** If a future Hermes renames or removes internals, only Layer 2 fails, and it
fails into a disabled state (Section 7). Layer 1 remains intact and testable, so a repair
touches one small file rather than the whole plugin.

---

## 5. Layer 1 — `NovitaEnvironment`

```python
class NovitaEnvironment(BaseEnvironment):
    _stdin_mode = "heredoc"     # SDK has no real stdin (base default is "pipe")
    _snapshot_timeout = 60      # cold start / pause-resume can be slow (base default 30)

    def __init__(self, template: str, cwd: str = "/home/user", timeout: int = 60,
                 cpu: int = 1, memory: int = 4096,
                 persistent_filesystem: bool = True, task_id: str = "default"): ...
```

### 5.1 Construction

1. **Resolve config.** `template`, `cpu`, `memory`, `container_persistent`, and
   `novita_timeout` come from `config.py` (Section 8).
2. **Resume-or-create.**
   - If `persistent_filesystem`: search with
     `novita.sandbox.list(query=SandboxQuery(state=[RUNNING, PAUSED]),
     metadata={"hermes_task_id": task_id})` → `.next_items()`; on a match,
     `novita.sandbox.connect(sandbox_id, timeout=novita_timeout)`.
   - Otherwise, or when no match exists:
     `novita.sandbox.create(template, timeout=novita_timeout,
     on_timeout="pause" if persistent else "kill",
     auto_resume=persistent, metadata={"hermes_task_id": task_id}, envs=<forwarded>)`.
   - Wrap the whole search in `try/except` and fall through to create (mirrors
     `daytona.py:89-131`, including its legacy-list fallback and its defensive logging).
3. **Resolve remote `$HOME`.** Run `echo $HOME`; if it differs and `cwd` was the default,
   rebind `self.cwd` (mirrors `daytona.py:132-142`).
4. **`FileSyncManager`** wired with the four callbacks in 5.3.
   `self._sync_manager.sync(force=True)` then `self.init_session()`.
5. **Dependency check.** `novita_sandbox` is imported lazily inside `__init__` so the SDK is
   only required when the backend is selected. Preferred path: register a
   `terminal.novita` group in `lazy_deps.LAZY_DEPS` (Section 6, patch 7) and call
   `ensure("terminal.novita", prompt=False)` the way `daytona.py:54-60` does. Fallback when
   patch 7 is unavailable: import directly and raise `ImportError` with actionable
   remediation text pointing at `hermes novita-sandbox doctor`.

### 5.2 `_run_bash`

```python
def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
    sandbox, lock = self._sandbox, self._lock

    def cancel():
        with lock:
            try:
                sandbox.pause()          # preserve the filesystem on interrupt
            except Exception:
                pass

    shell_cmd = f"bash -l -c {shlex.quote(cmd_string)}" if login \
           else f"bash -c {shlex.quote(cmd_string)}"

    def exec_fn() -> tuple[str, int]:
        try:
            r = sandbox.commands.run(shell_cmd, timeout=timeout)
            return (r.stdout or "", r.exit_code)
        except CommandExitException as e:      # MANDATORY — see Section 3
            return (e.stdout or "", e.exit_code)

    return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)
```

- `_stdin_mode = "heredoc"` means base's `_embed_stdin_heredoc` folds any `stdin_data` into
  the command string, so `stdin_data` needs no special handling here — matching Daytona.
  (`stdin_data` is accepted for signature compatibility; verify during implementation that
  base never routes it to a real pipe for heredoc backends.)
- **Decision: `cancel()` pauses rather than kills.** An interrupt must not destroy the
  user's filesystem. `_before_execute` resumes on the next command. Daytona's equivalent
  calls `sandbox.stop()` for the same reason (`daytona.py:226-231`).
  *Alternative considered:* `commands.run(..., background=True)` + `handle.kill()` to abort
  only the running command. Rejected as the default because it changes the exec contract and
  adds a handle-lifecycle to manage; noted in Section 12 for possible refinement.
- `timeout` is passed through to the SDK **and** honored by base's own wrapper. `timeout=0`
  means "no SDK timeout".

### 5.3 File-sync callbacks

Wired into `FileSyncManager(get_files_fn=lambda: iter_sync_files(f"{remote_home}/.hermes"),
upload_fn=..., delete_fn=..., bulk_upload_fn=..., bulk_download_fn=...)` exactly as
`daytona.py:144-151`.

| Callback | Implementation |
|---|---|
| `upload(host, remote)` | `mkdir -p <parent>` via `commands.run`, then `files.write(remote, open(host, "rb"))` |
| `bulk_upload(files)` | `mkdir -p` all `unique_parent_dirs(files)`, then `files.write_files([{"path": remote, "data": <bytes>}, ...])` |
| `bulk_download(dest)` | `commands.run("tar cf /tmp/.hermes_sync.<pid>.tar -C / <rel>")` → **`download_url(path)`** → `httpx.get(url).content` (bytes) → write to `dest` → `commands.run("rm -f …")` |
| `delete(paths)` | `commands.run(quoted_rm_command(paths))` |

PID-suffixed remote temp path mirrors `daytona.py:187` so concurrent `sync_back` calls for
the same sandbox cannot collide.

> **CORRECTED BY LIVE PROBE.** The original design read the tar back with `files.read(path)`.
> The probe proved `files.read()` returns **`str`**, not bytes — a tar fetched that way comes
> back decoded and is **not binary-safe**. The verified working path is
> `sandbox.download_url(path)` → HTTP GET, which the probe confirmed returns `200`,
> `10240` bytes, with the `ustar` magic intact at offset 257.
> Signature: `download_url(self, path, user=None, use_signature_expiration=None) -> str`.
> **`files.read` must never be used for binary payloads.**

### 5.4 Lifecycle

- `_before_execute()` — under `self._lock`: if paused, resume (`connect`); then
  `set_timeout(novita_timeout)` to keep an active session alive past the 5-minute default;
  then `self._sync_manager.sync()`. Mirrors `daytona.py:213-217` plus the timeout refresh.
- `cleanup()` — under `self._lock`, and after the `self._sandbox is None` guard (matching
  `daytona.py:244-270` to avoid a `sync_back` retry storm against a nil sandbox):
  1. `self._sync_manager.sync_back()` — **must run before teardown**, and must be wrapped in
     try/except that only warns (a failed sync_back must not strand the sandbox).
  2. `pause()` when `persistent_filesystem`, else `kill()`.
  3. `self._sandbox = None`.

Ordering (`sync_back` strictly before teardown) is asserted by `tests/tools/test_sync_back_backends.py:385-407`
for Daytona and is equally required here.

---

## 6. Layer 2 — injection and capability probes

`inject.py` exports `install(ctx) -> InjectionReport`. The report records, per patch:
`applied` / `skipped` / `failed`, the target's resolved identity, and the reason.

Every patch follows the same three steps: **locate** the target defensively with `getattr`,
**validate its shape** (callable / `frozenset` / `dict` / object with expected attribute),
**apply** — recording the outcome at each step. No patch may raise.

| # | Target | Method | Failure impact |
|---|---|---|---|
| 1 | `tools.terminal_tool._create_environment` | Wrap: if `env_type == "novita"` construct `NovitaEnvironment(...)` from config, else delegate to the original | **Pivotal** — backend unavailable |
| 2 | terminal tool `check_fn` | `registry.get_entry("terminal")` → replace `check_fn` to accept `novita` → `invalidate_check_fn_cache()` | **Pivotal** — else terminal reports unavailable |
| 3 | `agent.prompt_builder._REMOTE_TERMINAL_BACKENDS` | Rebind to `frozenset(orig \| {"novita"})` | Agent's system prompt describes the local host → misleading |
| 4 | `agent.prompt_builder._BACKEND_FALLBACK_DESCRIPTIONS` | `d["novita"] = "a Novita sandbox (Linux)"` | Cosmetic |
| 5 | `tools.env_probe._REMOTE_BACKENDS` | Rebind with `"novita"` added | Degraded env probing |
| 6 | `tools.skills_tool._REMOTE_ENV_BACKENDS` | Rebind with `"novita"` added | Degraded skill path resolution |
| 7 | `tools.lazy_deps.LAZY_DEPS` | `d["terminal.novita"] = ("novita-sandbox>=2.1.0,<3",)` | Plugin installs its own dependency |

**Why patch 1 is reliable.** Every consumer imports the factory *inside a function*, so the
name is re-resolved from the module on each call:
- `tools/terminal_tool.py:2036` — bare `_create_environment(...)` → module global
- `tools/code_execution_tool.py:608` — `from tools.terminal_tool import (… _create_environment …)` inside `_get_or_create_env`
- `tools/file_tools.py:745` — same pattern inside its env-accessor

Wrapping `tools.terminal_tool._create_environment` therefore intercepts **all** call paths.

**Why patches 3/5/6 are rebindable.** They are module-level `frozenset`/`dict` names consumed
as `name in _REMOTE_TERMINAL_BACKENDS`; rebinding the module global is picked up by those
`in` checks. `_BACKEND_FALLBACK_DESCRIPTIONS` is a `dict` and is mutated in place.

**Patch 2 detail.** `check_terminal_requirements` is passed as `check_fn` to
`registry.register(...)` at `tools/terminal_tool.py:2743`, so it is bound at registration
time; rebinding the module attribute alone would not affect the registry. The patch therefore
goes through the registry entry. `tools/__init__.py:20-22` and
`tools/terminal_tool.py:2642` call it by name and are covered by rebinding the module
attribute as well — do both, and call `invalidate_check_fn_cache()` (`tools/registry.py:144`).

**Idempotency.** Each wrapper is tagged (e.g. `__novita_wrapped__ = True`); `install()` is a
no-op if the tag is already present, so repeated `register()` calls (or a duplicated plugin
discovery) cannot stack wrappers.

**No core writes.** `install()` only rebinds attributes in already-imported `sys.modules`
entries. It never writes a file into the Hermes tree.

---

## 7. Failure model

`register()` must never raise. Its body is wrapped in a broad `try/except` that logs and
returns; a plugin that crashes during discovery would be worse than one that does nothing.

Three outcomes:

1. **All patches applied** → `terminal.backend: novita` works fully.
2. **Partial** → the backend works with the noted degradations, all listed by
   `hermes novita-sandbox doctor`.
3. **Pivotal patches (1 or 2) failed** → the backend is marked unavailable and the plugin
   registers a `pre_tool_call` guard: when `terminal.backend` is `novita` but the backend is
   unavailable, the guard returns

   ```
   {"action": "block",
    "message": "novita backend unavailable on Hermes <version>: <probe> failed. "
               "Run `hermes novita-sandbox doctor`, or set terminal.backend: local."}
   ```

   instead of letting Hermes emit its confusing `Unknown TERMINAL_ENV: novita. Use 'local',
   'docker', …` error from `terminal_tool.py:1365-1372`.

**Guarantee:** no Hermes update can leave a *broken* system. The worst case is the terminal
reverting to the previously configured backend, one loud log line, and a doctor command that
names the exact drifted probe. "Degrades safely" — not "never breaks" — is the honest claim.

---

## 8. Configuration and secret

`~/.hermes/config.yaml`:

```yaml
terminal:
  backend: novita
  novita_template: <built-template-id>   # or "base"
  novita_timeout: 300                    # sandbox lifetime, seconds (see 12.6 + COST)
  novita_refresh_window: 300             # keep-alive bump applied per execute
  container_cpu: 2                       # NOT applied at create — template-baked (12.3)
  container_memory: 4096                 # NOT applied at create — template-baked (12.3)
  container_persistent: true
```

> **Note (12.3):** `container_cpu` / `container_memory` are read for **reporting and for the
> `install-template` build step only**. The Novita SDK cannot apply them at sandbox-create
> time, and runtime mutation (`hotplug_memory` / `resize`) returns HTTP 500. Sizing is fixed
> by the template the sandbox boots from.

> **COST.** A *running* sandbox is billed per second for vCPU + RAM; a *paused* one is
> billed for neither (only Persistent Storage, 60 GB free per account). Because sandboxes
> are created with `on_timeout: pause` + `auto_resume`, pausing is free and resumes
> transparently -- so a LONG timeout buys nothing and bills for idle time. The default is
> therefore 300s, not 3600s. `hermes novita-sandbox stop --all` reclaims on demand.
>
> **Note (12.6):** `novita_timeout` is a lifetime measured **from sandbox creation**, not a
> duration added by each call. `_before_execute` computes `elapsed = now - started_at` and
> calls `set_timeout(elapsed + novita_refresh_window)`, and only when that extends the current
> `end_at` — otherwise a long-running session could have its deadline *shortened*.

`config.py` resolves these itself — preferring `hermes_cli.config.load_config()` when
importable, falling back to reading `get_hermes_home() / "config.yaml"` directly — with
hardcoded defaults for every key. The plugin therefore does not depend on Hermes' config
merge knowing about its keys, and keeps working if a future config-loader change drops
unknown keys.

All paths use `get_hermes_home()` / `display_hermes_home()` from `hermes_constants`; never a
hardcoded `~/.hermes` (the profile-safety rule in `AGENTS.md`).

**Secret:** `NOVITA_API_KEY` lives in `~/.hermes/.env` only (Hermes convention: `.env` is
secrets-only; non-secret settings belong in `config.yaml`). It is read from the environment,
never written to `config.yaml`, never logged, and never embedded in code.

> **Action required by the user:** the API key shared during design was pasted in plaintext
> into a chat transcript. Rotate it at novita.ai and set the new value with
> `hermes config set NOVITA_API_KEY <new-key>`, or by editing `~/.hermes/.env` directly.

---

## 9. Known, accepted divergences from a core backend

Both are safe, and both are consequences of not editing core:

1. **No sandboxed-backend approval bypass.** `tools/approval.py:1229,1480,1797` hold inline
   `env_type in {"docker", "singularity", "modal", "daytona", …}` sets that treat sandboxed
   backends as lower-risk. These are function-local literals and cannot be rebound.
   Result: commands run under `backend: novita` still go through Hermes' normal approval
   prompts. **More friction, not less safety.**
2. **No per-task resource overrides.** The gate at `tools/terminal_tool.py:2013` does not
   know `novita`, so RL/benchmark per-task overrides (`resolve_task_overrides`) will not
   reach the backend. The plugin reads global `terminal.*` config instead.

Also cosmetic: `hermes setup`'s interactive backend menu, `hermes doctor`, `hermes status`,
the dashboard config-editor dropdown, and `hermes tips` will not list `novita`. The backend
is selected by `config.yaml` / `hermes config set terminal.backend novita`. `hermes
novita-sandbox doctor` covers the diagnostics those commands would have provided.

---

## 10. Testing

Tests live in the plugin directory, **not** in the Hermes tree (`git clean -fd` would remove
them).

**Invocation.** Verified: the Hermes venv has **no `pip`** (`No module named pip`) and **no
`pytest` binary** in `venv/bin/`. The repo's `scripts/run_tests.sh` wrapper is not usable for
plugin-local tests (it targets the in-tree suite and expects the repo venv to carry the dev
dependencies). Use `uv` (verified present on PATH) with
ephemeral test dependencies so the Hermes venv is not polluted:

```bash
uv run --python ~/.hermes/hermes-agent/venv/bin/python \
       --with pytest --with pytest-mock \
       pytest tests/ -q
```

This gives the plugin the Hermes venv's interpreter (needed for `tools.environments.base`
imports) while sourcing pytest from an ephemeral overlay.

**Offline unit tests — `tests/test_environment.py`** (mock the SDK; build the object with
`object.__new__(NovitaEnvironment)` and hand-set attributes, mirroring
`tests/tools/test_sync_back_backends.py:85-95`):

1. **`CommandExitException` unwrap** — a non-zero exit returns `(stdout, exit_code)` and does
   **not** propagate. Highest-priority test; guards the Section 3 trap.
2. Resume-vs-create: a metadata match in `list()` → `connect()`; no match → `create()`.
3. `create()` args include `on_timeout="pause"`, `auto_resume=True`, and the `hermes_task_id`
   metadata when persistent; `"kill"` when not.
4. Bulk-download command shape: `tar cf`, PID-suffixed `/tmp/.hermes_sync.<pid>.tar`, the
   `.hermes` relative path; a trailing `rm -f`; and `dest` written exactly once.
5. `cleanup()` ordering: `sync_back` strictly before `pause`/`kill` (call-order list).
6. `cleanup()` tolerates a failing `sync_back` (warns, still tears down).
7. `FileSyncManager` wiring: the four callbacks are passed, and `bulk_download_fn` is ours.
8. `_stdin_mode == "heredoc"` and login/non-login shell command construction.

**Probe tests — `tests/test_inject.py`** (the tests that actually protect the durability
requirement): drive `install()` against synthetic modules where targets are (a) present and
correctly shaped, (b) **renamed**, (c) **missing**, (d) present but the **wrong type**
(e.g. `_REMOTE_TERMINAL_BACKENDS` replaced by a `list`). Assert for each: no exception
escapes `install()`, the report marks the patch `failed` with a reason, pivotal failures mark
the backend unavailable, and `install()` twice applies exactly one wrapper (idempotency).

**Live tests — `tests/test_live_novita.py`**, skipped at module level unless `NOVITA_API_KEY`
is set (mirrors `tests/integration/test_daytona_terminal.py:15-18`): `echo`, `python3
--version`, **non-zero exit code 42 surfaces as `exit_code == 42`**, `uname` sanity,
filesystem persistence across pause→connect, and isolation between two distinct `task_id`s.

**Manual end-to-end** (documented in the plugin README, performed once during
implementation): with `backend: novita`, confirm that `terminal`, `read_file`, `write_file`,
`patch`, `search_files`, and `execute_code` all route into the sandbox — i.e. that patch 1
covers the file tools and code-execution paths, not just `terminal`.

---

## 11. Out of scope (YAGNI)

- No core PR, no `tools/environments/novita.py`, no `ctx.register_environment_backend`.
- No `hermes setup` / `doctor` / `status` / dashboard integration (Section 9).
- No custom resource per-task overrides (Section 9).
- No desktop-automation / code-interpreter / `desktop` template support — this is a **shell**
  backend built on `sandbox.commands.run`.
- No snapshot management, template GC, or sandbox-cost accounting.
- No multi-account API-key pooling.
- No changes to `FileSyncManager` or any file-sync semantics beyond supplying callbacks.

---

## 12. Open items — RESOLVED against the live API (2026-09-23)

All items below were **settled by live probe**, not left to inference. Probes are committed
at `probes/integration_check.py` (plus the one-shot API probes that produced the
findings below, removed once their results were encoded in code and here).
Installed SDK: `novita-sandbox==2.1.1` (its `__version__` attribute reports a stale `1.0.0`;
trust the distribution version).

**Three original design assumptions were wrong and are corrected here.**

### 12.1 Confirmed as designed

**`CommandExitException` is real and carries the result fields.** Found at
`novita_sandbox.CommandExitException`. Its MRO is
`CommandExitException → SandboxException → Exception → BaseException`, **and also
`CommandResult`** — so it inherits `stdout`, `stderr`, `exit_code`, `error` directly.
Verified: `commands.run("echo before; exit 42")` raised with `.exit_code == 42`,
`.stdout == 'before\n'`, `.error == 'exit status 42'`. The §5.2 `except` unwrap is
**mandatory and correct**; it is the first thing the test suite must cover.

**Success shape.** `commands.run` returns a `CommandResult` with `.stdout`, `.stderr`,
`.exit_code == 0`, `.error == ''`.

**Pause/resume works.** `pause()` → `get_info().state == SandboxState.PAUSED`. This is the
persistence mechanism.

**`SandboxState`** is a str-subclass with `RUNNING` and `PAUSED`.

### 12.2 CORRECTED — binary download must not use `files.read`

`files.read()` returns **`str`**, not bytes. A tar fetched that way comes back decoded and is
**not binary-safe**. Verified working alternative: `sandbox.download_url(path)` → HTTP GET
returned `200`, `10240` bytes, `ustar` magic intact at offset 257.
Signature: `download_url(self, path, user=None, use_signature_expiration=None) -> str`.
`bulk_download` therefore uses `download_url` + `httpx` (see §5.3). `files.read` must never
touch a binary payload.

### 12.3 CORRECTED — resources are template-baked, not settable at create

The SDK's `Sandbox.create` signature has **no** `cpu_count` / `memory_mb` parameter. It
accepts `template`, `image`, `build`, `timeout`, `metadata`, `envs`, `secure`,
`allow_internet_access`, `auto_pause`, `mcp`, `network`, `secret_envs`, `lifecycle`,
`volume_mounts`, `node_id`.

Runtime mutation was attempted and **failed**:
- `hotplug_memory(4096)` → `SandboxException: 500: Error hotplugging memory on sandbox`
- `resize(memory_mib=4096)` → same `500`

The `base` template yields `cpu_count=2, memory_mb=512`.

**Consequence:** `terminal.container_cpu` / `container_memory` **cannot** be applied at
create time — sizing must go through `Template.build(..., cpu_count=..., memory_mb=...)`.
512 MB is low for agent work (installs, builds), so the parity template build is
**required, not optional** — but for *sizing* reasons, not toolchain reasons (see 12.5).
Account metadata reported `resource_pools: ["free"]`, so hotplug may be a paid-tier feature;
that is consistent with the 500s.

### 12.3b COST — running bills, paused does not, and `connect()` resumes

Pricing: a **running** sandbox is billed per second for vCPU + RAM (billing stops when it
stops); a **paused** one is billed for neither, only Persistent Storage — 60 GB free per
account, against a ~24 MB synced `.hermes` tree. At `base` sizing (2 vCPU / 512 MiB) that is
~$0.0000212/s ≈ $0.076/hour ≈ $1.83/day **if left running**.

So the only cost lever is how long a sandbox stays *running* after the last command. Two
consequences are encoded in the implementation:

1. **The lifetime default is 300s, not 3600s.** An earlier 3600s default (this spec's own
   §8 example, and what `setup` pinned into config.yaml) billed up to an hour of idle time
   for nothing, since pausing is free and we resume explicitly. `novita_timeout` no longer
   appears in the inserted setup handler precisely so it cannot be pinned into a user's
   config.yaml and then stop responding to a changed default.
2. **`auto_resume` is `false`.** `sandbox.connect()` RESUMES a paused sandbox as a side
   effect, so with auto-resume on, any stray SDK call or HTTP request wakes a paused sandbox
   and silently restarts billing. `_ensure_ready()` already resumes explicitly, so
   auto-resume adds nothing and is a cost trap. Corollary: `sandbox.list()` does NOT resume,
   but connecting to inspect a sandbox does — `stop` therefore skips already-paused
   sandboxes rather than waking them to pause them again.

### 12.4 CORRECTED — `lifecycle` takes a plain dict of strings

`create(..., lifecycle={"on_timeout": "pause", "auto_resume": True})` works and is confirmed
by readback: `get_info().lifecycle == {'on_timeout': SandboxOnTimeout.PAUSE, 'auto_resume': True}`.
`SandboxOnTimeout` is **not** importable from `novita_sandbox` nor from
`novita_sandbox.core.sandbox.sandbox_api` — pass the strings `"pause"` / `"kill"`.

Default lifecycle is `{'on_timeout': KILL, 'auto_resume': False}` with a 5-minute default
lifetime, so persistence must be requested explicitly.

### 12.5 `base` toolchain is already sufficient

`$HOME=/root`, `whoami=root`, `pwd=/root`, Linux 6.1.158+, bash 5.2.15,
**python3 3.11.6, node v20.9.0, git 2.39.5, tar 1.34, sha1sum/sha256sum present**.

This is materially close to `nikolaik/python-nodejs:python3.11-nodejs20`. The custom template
is therefore needed **only to raise cpu/memory** above the `base` 2 vCPU / 512 MB.

`template.list(template_type="template_build")` returned `total=0` — nothing has been built
on this account, and `base` is a system template. **Whether this account may build templates
(free tier) remains the one unverified item;** the first build attempt settles it.

### 12.6 `set_timeout` is seconds, measured from CREATION (not from the call)

Verified arithmetic: `create(timeout=300)` → `end_at = created + 300`.
Then `set_timeout(600)` moved `end_at` to `created + 600`, i.e. the observed delta was
**+303 s, not +600 s**.

**Implementation consequence:** a naive `set_timeout(window)` on each execute can *shrink* an
existing deadline. To keep an active session alive, compute
`elapsed = now - info.started_at` and call `set_timeout(elapsed + window)`, and only ever
call it when the resulting deadline is longer than the current `end_at`.

### 12.7 Class-level SDK calls lose auth unless the key is exported

With the key passed only to the `Novita(...)` client, bare classmethods raised
`AuthenticationException: API key is required…`:

```python
Sandbox.list(query=...)        # AuthenticationException
```

Setting `os.environ["NOVITA_API_KEY"]` before the call fixed it. **This would have been a
silent production failure** — the environment-resolution path (§5.1 step 5, §8) must export
`NOVITA_API_KEY` into the process environment before any class-level SDK call, in addition to
constructing the client.

### 12.8 The paginator raises on exhaustion

`pag.next_items()` raises `Exception("No more items to fetch")` rather than returning `[]`,
so the obvious loop **does not terminate normally**:

```python
while True:
    items = pag.next_items()   # raises once the pages run out
    if not items:
        break
```

Use `has_next`, or catch that exception explicitly.

### 12.9 Metadata filtering is unreliable — filter client-side

`SandboxQuery(state=[PAUSED], metadata={"hermes_probe3": "1"})` returned **zero** results,
while a state-only query, run moments later, found the very same sandbox whose
`metadata` readback contained exactly that key/value pair.

**Consequence:** `resume-or-create` lists by **state only**, then matches
`metadata.get("hermes_task_id")` in Python. Do not depend on server-side metadata filtering.

### 12.10 Verified sandbox surface (useful beyond the above)

- `sandbox.commands`: `run`, `kill`, `list`, `connect`, `send_stdin`
- `sandbox.files`: `read`, `write`, `write_files`, `list`, `**make_dir**`, `remove`, `rename`,
  `exists`, `get_info`, `watch_dir`
- `sandbox`: `pause`, `beta_pause`, `set_timeout`, `connect`, `kill`, `get_info`,
  `**download_url**`, `**upload_url**`, `git`, `reset`, `resize`, `hotplug_memory`,
  `create_snapshot`, `list_snapshots`, `get_quota`, `get_metrics`, `get_events`,
  `set_network`, `mount_volume`, **`pty`**, `is_running`, `default_template`,
  `default_sandbox_timeout`

Two consequences worth carrying into implementation:

1. **`files.make_dir` exists** — prefer it over shelling out to `mkdir -p` for upload parents
   (§5.3), removing a round-trip and a quoting hazard.
2. **`sandbox.pty` and `commands.send_stdin` exist** — real interactive stdin is available
   even though `_stdin_mode = "heredoc"` is retained for base-class compatibility. This is
   the escape hatch if heredoc folding ever proves lossy for a command.

### 12.11 Still genuinely open (cheap to settle later, none architectural)

1. **Template build acceptance + quota on this account** (12.5) — settled by the first
   `hermes novita-sandbox install-template` run.
2. **Resume latency** — not yet measured; determines whether bumping `novita_timeout` beats
   refreshing per-execute (§5.4).
3. **`files.make_dir` on nested paths** — verify it creates parents like `mkdir -p`.
4. **`use_signature_expiration` for `download_url`** — the default presigned-URL TTL is
   unknown; set it explicitly for the bulk-download path.
## 13. File manifest

| File | Purpose | ~Lines |
|---|---|---|
| `plugin.yaml` | manifest (`kind: standalone`) | 15 |
| `__init__.py` | `register(ctx)`; calls `inject.install(ctx)` + `cli.register` | 60 |
| `environment.py` | `NovitaEnvironment(BaseEnvironment)` | 260 |
| `inject.py` | 7 probes + wrappers + `InjectionReport` + unavailability guard | 220 |
| `config.py` | config + `NOVITA_API_KEY` resolution, defaults | 90 |
| `cli.py` | `hermes novita-sandbox doctor\|install-template` | 140 |
| `tests/test_environment.py` | offline unit tests | 260 |
| `tests/test_inject.py` | probe / durability tests | 200 |
| `tests/test_live_novita.py` | live integration (opt-in) | 150 |
| `README.md` | install, configure, verify, troubleshoot | 120 |
| **Total** | | **~1515** |

Figures are estimates to set expectations; they are not acceptance criteria.
