"""Unit tests for NovitaEnvironment against a mocked SDK.

No network. The live behaviours (real exception class, real transport) are
covered by tests/test_live_novita.py; these lock in the logic that the design
spec's probe results depend on.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from novita_env import (
    _MAX_BYTES_PER_BATCH,
    _MAX_FILES_PER_BATCH,
    NovitaEnvironment,
    _is_transport_error,
    iter_paginator,
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


class _FakeExitError(Exception):
    """Stand-in for novita_sandbox.CommandExitException.

    The real class cannot be constructed directly (it is a CommandResult
    subclass with an SDK-defined constructor). The live test asserts the real
    class carries these attributes; here we only need the field contract.
    """

    def __init__(self, stdout="", stderr="", exit_code=1, error="boom"):
        super().__init__(error)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.error = error


@pytest.fixture
def exit_error(monkeypatch):
    """Patch the name _exec resolves at call time."""
    import novita_sandbox

    monkeypatch.setattr(novita_sandbox, "CommandExitException", _FakeExitError, raising=False)
    return _FakeExitError


def make_env(**overrides) -> NovitaEnvironment:
    env = object.__new__(NovitaEnvironment)
    env._sandbox = overrides.get("sandbox") or MagicMock()
    env._lock = threading.Lock()
    env._persistent = overrides.get("persistent", True)
    env._task_id = overrides.get("task_id", "t1")
    env._template = overrides.get("template", "base")
    env._lifetime = overrides.get("lifetime", 3600)
    env._refresh_window = overrides.get("refresh_window", 3600)
    env._client = overrides.get("client") or MagicMock()
    env._sync_manager = overrides.get("sync_manager", MagicMock())
    env._remote_home = "/root"
    return env


class _Info:
    def __init__(self, state="running", started_ago=10, end_in=1000):
        now = datetime.now(timezone.utc)
        self.state = state
        self.started_at = now - timedelta(seconds=started_ago)
        self.end_at = now + timedelta(seconds=end_in)


# ----------------------------------------------------------------------
# the critical trap: non-zero exit must not raise
# ----------------------------------------------------------------------


def test_exec_unwraps_command_exit_exception(exit_error):
    """A non-zero exit returns (output, code) instead of propagating.

    This is the single most important behaviour: Novita raises where Daytona
    returns, so without the unwrap every benign failure (grep no-match,
    test -f, exit 1) would look like a crash to Hermes.
    """
    env = make_env()
    env._sandbox.commands.run.side_effect = exit_error(
        stdout="before\n", stderr="", exit_code=42
    )

    output, code = env._exec("echo before; exit 42", 60)

    assert code == 42
    assert "before" in output


def test_exec_returns_zero_exit_code_on_success(exit_error):
    env = make_env()
    result = MagicMock(stdout="hello\n", stderr="", exit_code=0)
    env._sandbox.commands.run.return_value = result

    output, code = env._exec("echo hello", 60)

    assert code == 0
    assert output == "hello\n"


def test_exec_combines_stderr_so_output_is_not_lost(exit_error):
    env = make_env()
    result = MagicMock(stdout="out\n", stderr="warn\n", exit_code=0)
    env._sandbox.commands.run.return_value = result

    output, _ = env._exec("cmd", 60)

    assert "out" in output
    assert "warn" in output


def test_exec_exit_exception_also_carries_stderr(exit_error):
    env = make_env()
    env._sandbox.commands.run.side_effect = _FakeExitError(
        stdout="partial\n", stderr="fatal\n", exit_code=3
    )

    output, code = env._exec("cmd", 60)

    assert code == 3
    assert "partial" in output
    assert "fatal" in output


def test_exec_adds_grace_to_sdk_timeout(exit_error):
    env = make_env()
    env._sandbox.commands.run.return_value = MagicMock(stdout="", stderr="", exit_code=0)

    env._exec("cmd", 60)

    _, kwargs = env._sandbox.commands.run.call_args
    assert kwargs["timeout"] > 60, "SDK timeout must exceed the Hermes-side timeout"


def test_exec_zero_timeout_means_no_sdk_timeout(exit_error):
    env = make_env()
    env._sandbox.commands.run.return_value = MagicMock(stdout="", stderr="", exit_code=0)

    env._exec("cmd", 0)

    _, kwargs = env._sandbox.commands.run.call_args
    assert kwargs["timeout"] == 0


# ----------------------------------------------------------------------
# transport resilience
# ----------------------------------------------------------------------


def test_exec_retries_once_after_transport_error(exit_error):
    env = make_env()
    env._sandbox.commands.run.side_effect = [
        BrokenPipeError("broken pipe"),
        MagicMock(stdout="recovered\n", stderr="", exit_code=0),
    ]
    env._reconnect = MagicMock()

    output, code = env._exec("cmd", 60)

    assert code == 0
    assert "recovered" in output
    env._reconnect.assert_called_once()
    assert env._sandbox.commands.run.call_count == 2


def test_exec_gives_up_after_one_transport_retry(exit_error):
    env = make_env()
    env._sandbox.commands.run.side_effect = BrokenPipeError("broken pipe")
    env._reconnect = MagicMock()

    with pytest.raises(BrokenPipeError):
        env._exec("cmd", 60)

    assert env._sandbox.commands.run.call_count == 2


def test_exec_does_not_retry_a_command_exit(exit_error):
    env = make_env()
    env._sandbox.commands.run.side_effect = _FakeExitError(stdout="x", exit_code=7)
    env._reconnect = MagicMock()

    _, code = env._exec("cmd", 60)

    assert code == 7
    env._reconnect.assert_not_called()
    assert env._sandbox.commands.run.call_count == 1


@pytest.mark.parametrize(
    "exc,expected",
    [
        (BrokenPipeError("x"), True),
        (ConnectionError("x"), True),
        (TimeoutError("x"), True),
        (ValueError("x"), False),
        (RuntimeError("x"), False),
    ],
)
def test_is_transport_error_classification(exc, expected):
    assert _is_transport_error(exc) is expected


def test_is_transport_error_recognises_httpx_module():
    class ReadError(Exception):
        pass

    ReadError.__module__ = "httpcore._exceptions"
    assert _is_transport_error(ReadError("broken"))


# ----------------------------------------------------------------------
# paginator exhaustion (probe 12.8)
# ----------------------------------------------------------------------


def test_iter_paginator_handles_exhaustion_by_raising():
    """next_items() raises instead of returning [] on this API."""
    paginator = MagicMock()
    paginator.has_next.return_value = True
    paginator.next_items.side_effect = [["a", "b"], Exception("No more items to fetch")]

    assert list(iter_paginator(paginator)) == ["a", "b"]


def test_iter_paginator_stops_on_has_next_false():
    paginator = MagicMock()
    paginator.has_next.return_value = False

    assert list(iter_paginator(paginator)) == []
    paginator.next_items.assert_not_called()


def test_iter_paginator_reraises_unrelated_errors():
    paginator = MagicMock()
    paginator.has_next.return_value = True
    paginator.next_items.side_effect = ValueError("auth exploded")

    with pytest.raises(ValueError):
        list(iter_paginator(paginator))


# ----------------------------------------------------------------------
# bulk upload chunking (broken-pipe fix)
# ----------------------------------------------------------------------


def _touch_files(tmp_path, count, size=10):
    paths = []
    for i in range(count):
        path = tmp_path / f"f{i}.txt"
        path.write_bytes(b"x" * size)
        paths.append((str(path), f"/root/.hermes/f{i}.txt"))
    return paths


def test_bulk_upload_chunks_by_file_count(tmp_path):
    env = make_env()
    env._sweep = None
    files = _touch_files(tmp_path, _MAX_FILES_PER_BATCH * 2 + 1)

    env._bulk_upload(files)

    assert env._sandbox.files.write_files.call_count == 3
    for call in env._sandbox.files.write_files.call_args_list:
        assert len(call.args[0]) <= _MAX_FILES_PER_BATCH


def test_bulk_upload_chunks_by_bytes(tmp_path):
    env = make_env()
    per_file = _MAX_BYTES_PER_BATCH // 2
    files = _touch_files(tmp_path, 4, size=per_file)

    env._bulk_upload(files)

    # Two 2MB files fill one 4MB batch exactly (the cap is exceeded, not met),
    # so four files must travel as two requests rather than one.
    assert env._sandbox.files.write_files.call_count == 2
    for call in env._sandbox.files.write_files.call_args_list:
        total = sum(len(entry["data"]) for entry in call.args[0])
        assert total <= _MAX_BYTES_PER_BATCH


def test_bulk_upload_creates_parent_dirs_in_bounded_chunks(tmp_path):
    from novita_env import _MAX_DIRS_PER_CMD

    env = make_env()
    files = []
    for i in range(_MAX_DIRS_PER_CMD + 5):
        path = tmp_path / f"n{i}.txt"
        path.write_bytes(b"y")
        files.append((str(path), f"/root/.hermes/d{i}/f.txt"))

    env._bulk_upload(files)

    mkdir_calls = [
        c for c in env._sandbox.commands.run.call_args_list
        if "mkdir" in str(c.args[0])
    ]
    assert len(mkdir_calls) >= 2, "mkdir command must be chunked, not one huge string"


def test_bulk_upload_noop_on_empty():
    env = make_env()
    env._bulk_upload([])
    env._sandbox.files.write_files.assert_not_called()


# ----------------------------------------------------------------------
# bulk download (binary-safe path, probe 12.2)
# ----------------------------------------------------------------------


def test_bulk_download_uses_download_url_and_writes_bytes(tmp_path, monkeypatch):
    env = make_env()
    env._sandbox.download_url.return_value = "https://example.invalid/files?x=1"
    payload = b"tar-bytes\x00\xff" * 100

    response = MagicMock(content=payload)
    response.raise_for_status = MagicMock()
    monkeypatch.setattr("httpx.get", lambda *a, **k: response)

    dest = tmp_path / "sync.tar"
    env._bulk_download(dest)

    assert dest.read_bytes() == payload, "must write raw bytes, not a decoded str"
    env._sandbox.download_url.assert_called_once()


def test_bulk_download_cleans_up_remote_temp_file(tmp_path, monkeypatch):
    env = make_env()
    env._sandbox.download_url.return_value = "https://example.invalid/f"
    response = MagicMock(content=b"x")
    response.raise_for_status = MagicMock()
    monkeypatch.setattr("httpx.get", lambda *a, **k: response)

    env._bulk_download(tmp_path / "d.tar")

    commands = [str(c.args[0]) for c in env._sandbox.commands.run.call_args_list]
    assert any("tar cf" in c for c in commands), "must tar the remote tree"
    assert any(c.startswith("rm -f") for c in commands), "must remove the remote tar"


def test_bulk_download_removes_temp_file_even_if_fetch_fails(tmp_path, monkeypatch):
    env = make_env()
    env._sandbox.download_url.return_value = "https://example.invalid/f"

    def boom(*a, **k):
        raise RuntimeError("network died")

    monkeypatch.setattr("httpx.get", boom)

    with pytest.raises(RuntimeError):
        env._bulk_download(tmp_path / "d.tar")

    commands = [str(c.args[0]) for c in env._sandbox.commands.run.call_args_list]
    assert any(c.startswith("rm -f") for c in commands)


def test_bulk_download_temp_path_is_pid_suffixed(tmp_path, monkeypatch):
    import os

    env = make_env()
    env._sandbox.download_url.return_value = "https://example.invalid/f"
    response = MagicMock(content=b"x")
    response.raise_for_status = MagicMock()
    monkeypatch.setattr("httpx.get", lambda *a, **k: response)

    env._bulk_download(tmp_path / "d.tar")

    tar_cmd = next(
        str(c.args[0]) for c in env._sandbox.commands.run.call_args_list
        if "tar cf" in str(c.args[0])
    )
    assert str(os.getpid()) in tar_cmd, "concurrent sync_back calls must not collide"


# ----------------------------------------------------------------------
# lifetime bumping (probe 12.6)
# ----------------------------------------------------------------------


def test_bump_timeout_extends_when_deadline_is_too_near():
    env = make_env(refresh_window=7200)
    env._sandbox.get_info.return_value = _Info(started_ago=10, end_in=100)

    env._bump_timeout(_Info(started_ago=10, end_in=100))

    env._sandbox.set_timeout.assert_called_once()
    assert env._sandbox.set_timeout.call_args.args[0] > 7200


def test_bump_timeout_never_shrinks_an_existing_deadline():
    """set_timeout is a lifetime from creation, so a naive call can move it back."""
    env = make_env(refresh_window=60)
    far_future = _Info(started_ago=10, end_in=100000)

    env._bump_timeout(far_future)

    env._sandbox.set_timeout.assert_not_called()


def test_bump_timeout_swallows_errors():
    env = make_env()
    env._sandbox.set_timeout.side_effect = RuntimeError("nope")

    env._bump_timeout(_Info(started_ago=10, end_in=1))  # must not raise


# ----------------------------------------------------------------------
# resume-or-create (probe 12.4 / 12.9)
# ----------------------------------------------------------------------


def test_resume_uses_connect_when_metadata_matches():
    env = make_env(persistent=True)
    env._find_existing = MagicMock(return_value="sb-123")
    env._forwarded_env = MagicMock(return_value={})

    env._resume_or_create()

    env._client.sandbox.connect.assert_called_once()
    env._client.sandbox.create.assert_not_called()


def test_create_requests_pause_and_auto_resume_when_persistent():
    env = make_env(persistent=True)
    env._find_existing = MagicMock(return_value=None)
    env._forwarded_env = MagicMock(return_value={})

    env._resume_or_create()

    _, kwargs = env._client.sandbox.create.call_args
    assert kwargs["lifecycle"] == {"on_timeout": "pause", "auto_resume": True}
    assert kwargs["metadata"]["hermes_task_id"] == "t1"


def test_create_requests_kill_when_not_persistent():
    env = make_env(persistent=False)
    env._find_existing = MagicMock(return_value=None)
    env._forwarded_env = MagicMock(return_value={})

    env._resume_or_create()

    _, kwargs = env._client.sandbox.create.call_args
    assert kwargs["lifecycle"] == {"on_timeout": "kill", "auto_resume": False}


def test_falls_back_to_create_when_connect_fails():
    env = make_env(persistent=True)
    env._find_existing = MagicMock(return_value="sb-dead")
    env._forwarded_env = MagicMock(return_value={})
    env._client.sandbox.connect.side_effect = RuntimeError("gone")

    env._resume_or_create()

    env._client.sandbox.create.assert_called_once()


def test_forwarded_env_only_includes_present_vars(monkeypatch):
    env = make_env()
    monkeypatch.setattr(
        "novita_config.get_setting",
        lambda key, default=None: ["PRESENT", "ABSENT"] if key == "env_passthrough" else default,
    )
    monkeypatch.setenv("PRESENT", "yes")
    monkeypatch.delenv("ABSENT", raising=False)

    assert env._forwarded_env() == {"PRESENT": "yes"}


# ----------------------------------------------------------------------
# cleanup ordering
# ----------------------------------------------------------------------


def test_cleanup_syncs_back_before_pausing():
    order = []
    env = make_env(persistent=True)
    env._sync_manager.sync_back.side_effect = lambda: order.append("sync_back")
    env._sandbox.pause.side_effect = lambda: order.append("pause")

    env.cleanup()

    assert order == ["sync_back", "pause"], "remote edits must be pulled before teardown"


def test_cleanup_kills_when_not_persistent():
    env = make_env(persistent=False)
    sandbox = env._sandbox

    env.cleanup()

    sandbox.kill.assert_called_once()
    sandbox.pause.assert_not_called()


def test_cleanup_continues_when_sync_back_fails():
    env = make_env(persistent=True)
    sandbox = env._sandbox
    env._sync_manager.sync_back.side_effect = RuntimeError("sync broke")

    env.cleanup()

    # A failed sync_back must not strand the sandbox.
    sandbox.pause.assert_called_once()


def test_cleanup_is_idempotent():
    env = make_env()
    sandbox = env._sandbox

    env.cleanup()
    env.cleanup()  # _sandbox is None by now

    sandbox.pause.assert_called_once()
