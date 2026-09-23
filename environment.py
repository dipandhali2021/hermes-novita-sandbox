"""Novita Agent Sandbox execution backend.

A plain ``BaseEnvironment`` subclass, the same shape as
``tools/environments/daytona.py``.  This module knows nothing about Hermes
internals and performs no monkeypatching -- routing ``backend: novita`` here is
``inject.py``'s job.  That separation is deliberate: if Hermes internals drift,
only the injector fails, and it fails into a disabled state while this file
stays intact and testable.

Novita SDK behaviours encoded here were established by live probe, not inferred
from documentation.  See docs/2026-09-23-novita-sandbox-backend-design.md
section 12 for the evidence behind each one.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shlex
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

from . import config as _config

logger = logging.getLogger(__name__)

# Grace added to the SDK-side command timeout so Hermes' own wrapper is the
# component that decides a command has timed out (mirrors daytona.py's note
# that the SDK timeout is unreliable as the primary control).
_SDK_TIMEOUT_GRACE = 10

_MAX_PAGES = 50

# Chunking for bulk upload. A full Hermes .hermes tree is ~550 files / ~22 MB
# (curator backup tarballs and the browser screenshot cache dominate it), and
# sending that as a single files.write_files() call drops the envd connection
# with a broken-pipe ReadError. Batches are capped by both count and bytes.
_MAX_FILES_PER_BATCH = 40
_MAX_BYTES_PER_BATCH = 4 * 1024 * 1024
# `mkdir -p a b c ...` is built as one shell command; keep it well under any
# command-length limit (a full tree yields ~200 parents).
_MAX_DIRS_PER_CMD = 40


def _is_transport_error(exc: BaseException) -> bool:
    """True for network/transport failures (as opposed to a command result).

    A command's non-zero exit is a *result*, never an error here -- it arrives
    as CommandExitException and is unwrapped before this is consulted.
    """
    if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
        return True
    label = f"{type(exc).__module__}.{type(exc).__name__}".lower()
    return any(
        token in label
        for token in ("httpx", "httpcore", "socket", "connection", "protocol", "timeout")
    )


def iter_paginator(paginator):
    """Iterate a Novita paginator, tolerating its exhaustion behaviour.

    Live probe (spec 12.8): ``next_items()`` **raises**
    ``Exception("No more items to fetch")`` instead of returning an empty list,
    so the obvious ``while True: items = p.next_items(); if not items: break``
    loop does not terminate normally.
    """
    for _ in range(_MAX_PAGES):
        try:
            has_next = getattr(paginator, "has_next", None)
            if has_next is not None:
                more = has_next() if callable(has_next) else has_next
                if more is False:
                    return
        except Exception:  # noqa: BLE001 - has_next is advisory only
            pass

        try:
            items = paginator.next_items()
        except Exception as e:  # noqa: BLE001
            if "no more items" in str(e).lower():
                return
            raise
        if not items:
            return
        for item in items:
            yield item


def _ensure_sdk() -> None:
    """Import the SDK, installing it first if Hermes' lazy-deps can."""
    try:
        import novita_sandbox  # noqa: F401
        return
    except ImportError:
        pass

    # Preferred: the injector registers a terminal.novita group in
    # tools.lazy_deps.LAZY_DEPS, which gives us Hermes' allowlist + TTY prompt.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure  # type: ignore

        _lazy_ensure("terminal.novita", prompt=False)
    except Exception as e:  # noqa: BLE001
        logger.debug("novita: lazy_deps install path unavailable: %s", e)

    try:
        import novita_sandbox  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "novita-sandbox is not installed.\n"
            "Install it with:\n"
            f"  uv pip install --python {os.sys.executable} 'novita-sandbox>=2.1.0,<3'\n"
            "or run:  hermes novita-sandbox setup"
        ) from e


class NovitaEnvironment(BaseEnvironment):
    """Novita Agent Sandbox backend, with pause/resume persistence.

    Spawn-per-call: each command runs through ``_ThreadedProcessHandle``
    wrapping the blocking SDK call.  ``cancel_fn`` pauses the sandbox so an
    interrupt preserves the user's filesystem.
    """

    # No real stdin channel: the SDK streams stdout/stderr separately. Base's
    # heredoc folding handles stdin_data inside the command string.
    _stdin_mode = "heredoc"
    # Cold start, and especially pause->resume, can exceed the 30s default.
    _snapshot_timeout = 60

    def __init__(
        self,
        template: str | None = None,
        cwd: str = "/root",
        timeout: int = 60,
        cpu: int | None = None,
        memory: int | None = None,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ):
        requested_cwd = cwd
        super().__init__(cwd=cwd, timeout=timeout)

        cfg = _config.terminal_config()
        self._template = template or cfg.get("novita_template") or "base"
        self._persistent = bool(persistent_filesystem)
        self._task_id = task_id
        self._lifetime = _config.get_int_setting("novita_timeout", 3600)
        self._refresh_window = _config.get_int_setting("novita_refresh_window", 3600)

        # cpu/memory are accepted for signature parity with the other backends'
        # factory call, but the Novita API cannot apply them at create time and
        # runtime mutation returns HTTP 500 (spec 12.3). Sizing is baked into
        # the template.
        for label, requested, key in (
            ("cpu", cpu, "container_cpu"),
            ("memory", memory, "container_memory"),
        ):
            if requested is None:
                continue
            configured = _config.get_int_setting(key, requested)
            if configured != requested:
                logger.debug("novita: ignoring %s=%s (template-baked)", label, requested)

        self._lock = threading.Lock()
        self._sandbox = None
        self._client = None
        self._sync_manager: FileSyncManager | None = None

        key = _config.export_api_key()
        if not key:
            raise ImportError(
                "NOVITA_API_KEY is not set. Add it to "
                f"{_config.hermes_home()}/.env or run: hermes novita-sandbox setup"
            )

        _ensure_sdk()
        from novita_sandbox import Novita

        self._client = Novita(api_key=key)
        self._sandbox = self._resume_or_create()

        # Resolve the remote home so file sync and cwd land in the right place.
        self._remote_home = "/root"
        try:
            home = (self._sandbox.commands.run("echo $HOME").stdout or "").strip()
            if home:
                self._remote_home = home
                if requested_cwd in {"~", "/root", "/home/user"}:
                    self.cwd = home
        except Exception as e:  # noqa: BLE001
            logger.debug("novita: could not resolve $HOME: %s", e)

        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._upload,
            delete_fn=self._delete,
            bulk_upload_fn=self._bulk_upload,
            bulk_download_fn=self._bulk_download,
        )
        # Seed sync is an optimisation, not a precondition: the environment is
        # usable without it and _before_execute() retries incrementally. A
        # failure here must not make the backend unusable.
        try:
            self._sync_manager.sync(force=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "novita: initial file sync failed (%s); continuing -- later "
                "syncs will retry incrementally",
                e,
            )
        self.init_session()

    # ------------------------------------------------------------------
    # Sandbox lifecycle
    # ------------------------------------------------------------------

    def _resume_or_create(self):
        if self._persistent:
            existing = self._find_existing()
            if existing:
                try:
                    sandbox = self._client.sandbox.connect(
                        existing, timeout=self._lifetime
                    )
                    logger.info(
                        "novita: resumed sandbox %s for task %s", existing, self._task_id
                    )
                    return sandbox
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "novita: could not resume sandbox %s (%s); creating a new one",
                        existing,
                        e,
                    )

        # lifecycle takes a plain dict of strings: SandboxOnTimeout is not
        # importable from the public surface (spec 12.4).
        lifecycle = {
            "on_timeout": "pause" if self._persistent else "kill",
            "auto_resume": bool(self._persistent),
        }
        envs = self._forwarded_env()
        sandbox = self._client.sandbox.create(
            self._template,
            timeout=self._lifetime,
            metadata={"hermes_task_id": self._task_id},
            lifecycle=lifecycle,
            **( {"envs": envs} if envs else {} ),
        )
        logger.info(
            "novita: created sandbox %s from template %s for task %s",
            sandbox.sandbox_id,
            self._template,
            self._task_id,
        )
        return sandbox

    def _forwarded_env(self) -> dict[str, str]:
        """Env vars named in terminal.env_passthrough, forwarded at create."""
        names = _config.get_setting("env_passthrough", []) or []
        if not isinstance(names, (list, tuple)):
            return {}
        out = {}
        for name in names:
            value = os.environ.get(str(name))
            if value:
                out[str(name)] = value
        return out

    def _find_existing(self) -> str | None:
        """Find this task's sandbox by state, then filter metadata client-side.

        Live probe (spec 12.9): a server-side metadata filter returned zero
        results for a sandbox whose metadata matched exactly, while a state-only
        query found it immediately. Metadata filtering is therefore done here.
        """
        try:
            from novita_sandbox import SandboxQuery, SandboxState

            paginator = self._client.sandbox.list(
                query=SandboxQuery(
                    state=[SandboxState.RUNNING, SandboxState.PAUSED]
                )
            )
            for info in iter_paginator(paginator):
                metadata = getattr(info, "metadata", None) or {}
                if metadata.get("hermes_task_id") == self._task_id:
                    return getattr(info, "sandbox_id", None)
        except Exception as e:  # noqa: BLE001
            logger.debug("novita: sandbox list failed: %s", e)
        return None

    def _ensure_ready(self) -> None:
        """Resume a paused sandbox and keep its lifetime extended."""
        sandbox = self._sandbox
        if sandbox is None:
            raise RuntimeError("novita: sandbox is gone")

        try:
            info = sandbox.get_info()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"novita: cannot reach sandbox: {e}") from e

        state = str(getattr(info, "state", "") or "").lower()
        if "paused" in state or "stopped" in state or "archived" in state:
            sandbox = self._client.sandbox.connect(
                sandbox.sandbox_id, timeout=self._lifetime
            )
            self._sandbox = sandbox
            logger.info("novita: resumed sandbox %s", sandbox.sandbox_id)
            info = sandbox.get_info()

        self._bump_timeout(info)

    def _bump_timeout(self, info) -> None:
        """Extend the sandbox lifetime, never shorten it.

        Live probe (spec 12.6): the timeout is a lifetime measured from sandbox
        *creation*, not a duration added per call. create(timeout=300) gave
        end_at = created+300, and set_timeout(600) then moved end_at to
        created+600 -- a delta of +303s, not +600s. Calling set_timeout(window)
        naively can therefore move a deadline *backwards*.
        """
        try:
            started = getattr(info, "started_at", None)
            end_at = getattr(info, "end_at", None)
            if started is None:
                return
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            elapsed = (now - started).total_seconds()
            target = elapsed + self._refresh_window
            if end_at is not None:
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=timezone.utc)
                if started + timedelta(seconds=target) <= end_at:
                    return  # already far enough out; never shrink

            self._sandbox.set_timeout(int(target))
            logger.debug("novita: extended lifetime to %ss from creation", int(target))
        except Exception as e:  # noqa: BLE001 - keep-alive is best-effort
            logger.debug("novita: set_timeout bump failed: %s", e)

    def _before_execute(self) -> None:
        with self._lock:
            self._ensure_ready()
        if self._sync_manager is not None:
            self._sync_manager.sync()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _sdk_timeout(self, timeout: int | None) -> int:
        try:
            value = int(timeout) if timeout is not None else 0
        except (TypeError, ValueError):
            value = 0
        # Novita treats timeout=0 as "no timeout"; only add grace to a real one.
        return value + _SDK_TIMEOUT_GRACE if value > 0 else 0

    def _exec(self, shell_cmd: str, timeout: int | None) -> tuple[str, int]:
        """Run one shell command, returning ``(combined_output, exit_code)``.

        Novita's SDK *raises* ``CommandExitException`` on any non-zero exit
        rather than returning the code (verified live: ``exit 42`` raised with
        ``.exit_code == 42``).  That exception also inherits ``CommandResult``,
        so ``stdout`` / ``stderr`` / ``exit_code`` are available on it.

        Without this unwrap, every benign non-zero exit -- ``grep`` with no
        match, ``test -f``, ``exit 1`` -- would propagate as a Python exception
        and break the whole command layer.

        Transport failures (dropped envd connection) are retried once after a
        reconnect: a cloud backend over HTTP will eventually see one, and a
        transient socket error must not fail the user's command.
        """
        from novita_sandbox import CommandExitException

        last_error: BaseException | None = None
        for attempt in (0, 1):
            try:
                result = self._sandbox.commands.run(
                    shell_cmd, timeout=self._sdk_timeout(timeout)
                )
                out = result.stdout or ""
                err = getattr(result, "stderr", "") or ""
                return (out + err, int(result.exit_code))
            except CommandExitException as e:
                out = getattr(e, "stdout", "") or ""
                err = getattr(e, "stderr", "") or ""
                return (out + err, int(getattr(e, "exit_code", 1)))
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt == 0 and _is_transport_error(e):
                    logger.warning("novita: transport error (%s); reconnecting", e)
                    try:
                        self._reconnect()
                        continue
                    except Exception as re:  # noqa: BLE001
                        logger.warning("novita: reconnect failed: %s", re)
                raise

        raise last_error if last_error is not None else RuntimeError("novita: exec failed")

    def _reconnect(self) -> None:
        """Re-establish the envd connection to the same sandbox."""
        with self._lock:
            sandbox = self._sandbox
            if sandbox is None:
                raise RuntimeError("novita: sandbox is gone")
            sandbox_id = getattr(sandbox, "sandbox_id", None)
            if not sandbox_id:
                raise RuntimeError("novita: sandbox has no id")
            self._sandbox = self._client.sandbox.connect(
                sandbox_id, timeout=self._lifetime
            )
        logger.info("novita: reconnected to sandbox %s", sandbox_id)

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        lock = self._lock

        def cancel() -> None:
            # Pause rather than kill: an interrupt must not destroy the user's
            # filesystem. _ensure_ready resumes on the next command. (daytona.py
            # calls sandbox.stop() for the same reason.)
            with lock:
                sandbox = self._sandbox
                if sandbox is None:
                    return
                try:
                    sandbox.pause()
                except Exception:  # noqa: BLE001
                    pass

        if login:
            shell_cmd = f"bash -l -c {shlex.quote(cmd_string)}"
        else:
            shell_cmd = f"bash -c {shlex.quote(cmd_string)}"

        def exec_fn() -> tuple[str, int]:
            return self._exec(shell_cmd, timeout)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    # ------------------------------------------------------------------
    # File sync callbacks (consumed by FileSyncManager)
    # ------------------------------------------------------------------

    def _make_dir(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        try:
            self._sandbox.files.make_dir(remote_dir)
        except Exception:  # noqa: BLE001 - falls back to shell mkdir -p
            self._sandbox.commands.run(f"mkdir -p {shlex.quote(remote_dir)}")

    def _make_dirs(self, parents: list[str]) -> None:
        """Create parent directories, chunked to bound the shell command length."""
        for start in range(0, len(parents), _MAX_DIRS_PER_CMD):
            chunk = parents[start : start + _MAX_DIRS_PER_CMD]
            if chunk:
                self._sandbox.commands.run(quoted_mkdir_command(chunk))

    def _upload(self, host_path: str, remote_path: str) -> None:
        parent = posixpath.dirname(remote_path)
        self._make_dir(parent)
        # Bytes, not a file object: verified working against the live API.
        self._sandbox.files.write(remote_path, Path(host_path).read_bytes())

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """Upload many files, batched by count *and* bytes.

        A single write_files() call carrying the whole tree (~550 files / ~22 MB
        for a typical Hermes home) drops the connection. Batches stay under both
        caps; a partial failure leaves earlier batches in place and the next
        incremental sync retries the rest.
        """
        if not files:
            return

        batches: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        current_bytes = 0
        for host_path, remote_path in files:
            try:
                size = os.path.getsize(host_path)
            except OSError:
                size = 0
            if current and (
                len(current) >= _MAX_FILES_PER_BATCH
                or current_bytes + size > _MAX_BYTES_PER_BATCH
            ):
                batches.append(current)
                current, current_bytes = [], 0
            current.append((host_path, remote_path))
            current_bytes += size
        if current:
            batches.append(current)

        for index, batch in enumerate(batches, start=1):
            parents = unique_parent_dirs(batch)
            if parents:
                self._make_dirs(parents)
            payload = [
                {"path": remote_path, "data": Path(host_path).read_bytes()}
                for host_path, remote_path in batch
            ]
            if payload:
                self._sandbox.files.write_files(payload)
            logger.debug(
                "novita: uploaded batch %d/%d (%d files)", index, len(batches), len(batch)
            )

    def _bulk_download(self, dest: Path) -> None:
        """Tar the remote .hermes tree and pull it down as bytes.

        ``files.read()`` returns ``str`` and is not binary-safe (spec 12.2), so
        the tar is fetched through the SDK's presigned ``download_url``. The
        probe verified a 200, byte-exact response with the ustar magic intact.

        The remote temp path is PID-suffixed so concurrent sync_back calls for
        the same sandbox cannot collide (mirrors daytona.py:187).
        """
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        remote_tar = f"/tmp/.hermes_sync.{os.getpid()}.tar"
        self._sandbox.commands.run(
            f"tar cf {shlex.quote(remote_tar)} -C / {shlex.quote(rel_base)}"
        )
        try:
            url = self._sandbox.download_url(remote_tar, use_signature_expiration=300)
            import httpx

            response = httpx.get(url, timeout=120, follow_redirects=True)
            response.raise_for_status()
            Path(dest).write_bytes(response.content)
        finally:
            try:
                self._sandbox.commands.run(f"rm -f {shlex.quote(remote_tar)}")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _delete(self, remote_paths: list[str]) -> None:
        if remote_paths:
            self._sandbox.commands.run(quoted_rm_command(remote_paths))

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        with self._lock:
            if self._sandbox is None:
                return

            # Inside the lock and after the None guard, so a sync_back can never
            # fire against a nil sandbox and trigger a retry storm
            # (mirrors daytona.py:244-270).
            if self._sync_manager is not None:
                logger.info("novita: syncing files back from sandbox...")
                try:
                    self._sync_manager.sync_back()
                except Exception as e:  # noqa: BLE001
                    logger.warning("novita: sync_back failed: %s", e)

            try:
                if self._persistent:
                    self._sandbox.pause()
                    logger.info(
                        "novita: paused sandbox %s (filesystem preserved)",
                        self._sandbox.sandbox_id,
                    )
                else:
                    self._sandbox.kill()
                    logger.info("novita: killed sandbox %s", self._sandbox.sandbox_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("novita: cleanup failed: %s", e)
            self._sandbox = None
