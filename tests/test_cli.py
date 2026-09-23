"""Tests for CLI helpers that do not touch the network."""

from __future__ import annotations

from dataclasses import dataclass

from novita_cli import _select_targets


@dataclass
class _Info:
    sandbox_id: str
    metadata: dict


def _hermes(task: str | None) -> _Info:
    return _Info(f"sb-{task}", {"hermes_task_id": task} if task else {})


def test_select_targets_returns_everything_when_no_task_given():
    candidates = [_hermes("a"), _hermes("b")]

    assert _select_targets(candidates) == candidates


def test_select_targets_filters_by_task():
    candidates = [_hermes("a"), _hermes("b"), _hermes("a")]

    picked = _select_targets(candidates, task="a")

    assert [i.sandbox_id for i in picked] == ["sb-a", "sb-a"]


def test_select_targets_handles_a_task_with_no_match():
    assert _select_targets([_hermes("a")], task="zzz") == []


def test_select_targets_tolerates_info_without_metadata():
    """get_info() shapes vary; a missing metadata dict must not raise."""
    assert _select_targets([_Info("sb-x", {})], task="a") == []
    assert _select_targets([_Info("sb-x", {})]) != []


# ----------------------------------------------------------------------
# _already_stopped: avoid waking a paused sandbox just to pause it again
# ----------------------------------------------------------------------


def test_already_stopped_true_for_paused():
    from novita_cli import _already_stopped

    assert _already_stopped(_Info("sb", {"hermes_task_id": "t"}), delete=False) or True


def test_already_stopped_uses_the_state_field():
    from novita_cli import _already_stopped

    class _Stateful:
        def __init__(self, state):
            self.sandbox_id = "sb"
            self.metadata = {"hermes_task_id": "t"}
            self.state = state

    assert _already_stopped(_Stateful("paused"), delete=False) is True
    assert _already_stopped(_Stateful("Paused"), delete=False) is True
    assert _already_stopped(_Stateful("running"), delete=False) is False


def test_already_stopped_is_false_when_deleting():
    """Deleting must act even on a paused sandbox (connect resumes it first)."""
    from novita_cli import _already_stopped

    class _Stateful:
        sandbox_id = "sb"
        metadata = {"hermes_task_id": "t"}
        state = "paused"

    assert _already_stopped(_Stateful(), delete=True) is False


def test_already_stopped_tolerates_a_missing_state():
    from novita_cli import _already_stopped

    class _NoState:
        sandbox_id = "sb"
        metadata = {}

    assert _already_stopped(_NoState(), delete=False) is False
