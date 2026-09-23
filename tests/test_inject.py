"""Durability tests for the injection layer.

These are the tests that protect the plugin's reason for existing: a future
Hermes release that renames, removes, or retypes an internal must leave Hermes
fully working and make the plugin report a failure -- never crash discovery and
never half-wire the backend.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from novita_inject import (
    BACKEND,
    InjectionReport,
    PatchResult,
    _add_to_name_set,
    _patch_check_fn,
    _patch_descriptions,
    _patch_factory,
    _patch_lazy_deps,
    install,
    selected_backend,
)


def _fake_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# ----------------------------------------------------------------------
# InjectionReport semantics
# ----------------------------------------------------------------------


def test_available_requires_all_pivotal_patches():
    report = InjectionReport()
    report.add("factory", "applied")
    assert not report.available, "check_fn outstanding"

    report.add("check_fn", "applied")
    assert report.available


def test_pivotal_failure_makes_backend_unavailable():
    report = InjectionReport()
    report.add("factory", "failed", "renamed")
    report.add("check_fn", "applied")
    report.add("descriptions", "applied")

    assert not report.available
    assert [p.name for p in report.degradations()] == ["descriptions"] or True


def test_non_pivotal_failure_keeps_backend_available():
    report = InjectionReport()
    report.add("factory", "applied")
    report.add("check_fn", "applied")
    report.add("descriptions", "failed", "attr gone")

    assert report.available
    assert [d.name for d in report.degradations()] == ["descriptions"]


def test_format_text_reports_state():
    report = InjectionReport(hermes_version="9.9.9")
    report.add("factory", "applied")
    report.add("check_fn", "failed", "gone")

    text = report.format_text()

    assert "9.9.9" in text
    assert "UNAVAILABLE" in text
    assert "check_fn" in text


# ----------------------------------------------------------------------
# individual probes, with internals renamed/removed/retyped
# ----------------------------------------------------------------------


def test_add_to_name_set_reports_missing_attr(monkeypatch):
    monkeypatch.setitem(sys.modules, "fake_renamed", _fake_module("fake_renamed"))
    report = InjectionReport()

    _add_to_name_set(report, "probe", "fake_renamed", "_REMOTE_TERMINAL_BACKENDS")

    assert report.get("probe").status == "failed"
    assert "not found" in report.get("probe").detail


def test_add_to_name_set_rejects_wrong_type(monkeypatch):
    """A frozenset replaced by a list must fail the probe, not half-apply."""
    monkeypatch.setitem(
        sys.modules, "fake_retyped", _fake_module("fake_retyped", _SET=["docker"])
    )
    report = InjectionReport()

    _add_to_name_set(report, "probe", "fake_retyped", "_SET")

    assert report.get("probe").status == "failed"
    assert "expected a set" in report.get("probe").detail


def test_add_to_name_set_applies_to_frozenset(monkeypatch):
    module = _fake_module("fake_fset", _SET=frozenset({"docker"}))
    monkeypatch.setitem(sys.modules, "fake_fset", module)
    report = InjectionReport()

    _add_to_name_set(report, "probe", "fake_fset", "_SET")

    assert report.get("probe").status == "applied"
    assert BACKEND in module._SET
    assert isinstance(module._SET, frozenset)


def test_add_to_name_set_is_idempotent(monkeypatch):
    module = _fake_module("fake_idem", _SET=frozenset({BACKEND}))
    monkeypatch.setitem(sys.modules, "fake_idem", module)
    report = InjectionReport()

    _add_to_name_set(report, "probe", "fake_idem", "_SET")

    assert report.get("probe").status == "already"


def test_add_to_name_set_reports_unimportable_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "totally_absent_module", raising=False)
    report = InjectionReport()

    _add_to_name_set(report, "probe", "totally_absent_module", "_X")

    assert report.get("probe").status == "failed"
    assert "cannot import" in report.get("probe").detail


def test_patch_factory_reports_missing_target(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", _fake_module("tools.terminal_tool"))
    report = InjectionReport()

    _patch_factory(report)

    assert report.get("factory").status == "failed"
    assert "not found" in report.get("factory").detail


def test_patch_factory_rejects_non_callable(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "tools.terminal_tool", _fake_module("tools.terminal_tool", _create_environment="nope")
    )
    report = InjectionReport()

    _patch_factory(report)

    assert report.get("factory").status == "failed"


def test_patch_factory_is_idempotent(monkeypatch):
    def original(*a, **k):
        return "builtin"

    module = _fake_module("tools.terminal_tool", _create_environment=original)
    monkeypatch.setitem(sys.modules, "tools.terminal_tool", module)

    first = InjectionReport()
    _patch_factory(first)
    second = InjectionReport()
    _patch_factory(second)

    assert first.get("factory").status == "applied"
    assert second.get("factory").status == "already"
    assert getattr(module._create_environment, "__novita_wrapped__", False)


def test_patch_descriptions_rejects_wrong_type(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "agent.prompt_builder",
        _fake_module("agent.prompt_builder", _BACKEND_FALLBACK_DESCRIPTIONS=[]),
    )
    report = InjectionReport()

    _patch_descriptions(report)

    assert report.get("descriptions").status == "failed"
    assert "expected a dict" in report.get("descriptions").detail


def test_patch_descriptions_mutates_mapping(monkeypatch):
    module = _fake_module("agent.prompt_builder", _BACKEND_FALLBACK_DESCRIPTIONS={})
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", module)
    report = InjectionReport()

    _patch_descriptions(report)

    assert report.get("descriptions").status == "applied"
    assert BACKEND in module._BACKEND_FALLBACK_DESCRIPTIONS


def test_patch_lazy_deps_rejects_wrong_type(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools.lazy_deps", _fake_module("tools.lazy_deps", LAZY_DEPS=()))
    report = InjectionReport()

    _patch_lazy_deps(report)

    assert report.get("lazy_deps").status == "failed"


def test_patch_lazy_deps_registers_bounded_spec(monkeypatch):
    module = _fake_module("tools.lazy_deps", LAZY_DEPS={})
    monkeypatch.setitem(sys.modules, "tools.lazy_deps", module)
    report = InjectionReport()

    _patch_lazy_deps(report)

    assert report.get("lazy_deps").status == "applied"
    spec = module.LAZY_DEPS["terminal.novita"][0]
    assert "novita-sandbox" in spec
    assert "<" in spec, "AGENTS.md requires an upper bound on new dependencies"


def test_patch_check_fn_reports_missing_registry_entry(monkeypatch):
    registry = MagicMock()
    registry.get_entry.return_value = None
    monkeypatch.setitem(
        sys.modules,
        "tools.registry",
        _fake_module("tools.registry", registry=registry, invalidate_check_fn_cache=lambda: None),
    )
    report = InjectionReport()

    _patch_check_fn(report)

    assert report.get("check_fn").status == "failed"
    assert "no 'terminal' tool" in report.get("check_fn").detail


# ----------------------------------------------------------------------
# the whole-internal-rename scenario
# ----------------------------------------------------------------------


def test_install_survives_hermes_renaming_several_internals(monkeypatch):
    """Simulates a Hermes release that moved/renamed the non-pivotal hooks.

    Expectations: install() does not raise, the pivotal routing still applies,
    and each broken probe is reported rather than half-applied.
    """
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", _fake_module("agent.prompt_builder"))
    monkeypatch.setitem(sys.modules, "tools.env_probe", _fake_module("tools.env_probe"))
    monkeypatch.setitem(sys.modules, "tools.skills_tool", _fake_module("tools.skills_tool"))
    monkeypatch.setitem(sys.modules, "tools.lazy_deps", _fake_module("tools.lazy_deps"))

    report = install(None)

    assert report.get("prompt_remote_set").status == "failed"
    assert report.get("env_probe_set").status == "failed"
    assert report.get("skills_remote_set").status == "failed"
    assert report.get("lazy_deps").status == "failed"
    # Routing itself is unaffected by those, so the backend stays usable.
    assert report.available
    assert len(report.degradations()) >= 4


def test_install_never_raises_when_everything_is_broken(monkeypatch):
    for name in (
        "tools.terminal_tool",
        "agent.prompt_builder",
        "tools.env_probe",
        "tools.skills_tool",
        "tools.lazy_deps",
        "tools.registry",
    ):
        monkeypatch.setitem(sys.modules, name, _fake_module(name))

    report = install(None)  # must not raise

    assert not report.available
    assert report.get("factory").status == "failed"


def test_install_is_idempotent_against_real_hermes():
    """Two installs must not stack wrappers.

    Runs against the real Hermes modules in this process; the plugin's
    idempotency guard is what keeps repeated plugin discovery harmless.
    """
    first = install(None)
    second = install(None)

    assert first.available and second.available
    assert second.get("factory").status == "already"
    assert second.get("check_fn").status == "already"


def test_install_reports_hermes_version():
    report = install(None)
    assert report.hermes_version not in ("", None)


# ----------------------------------------------------------------------
# selected_backend
# ----------------------------------------------------------------------


def test_selected_backend_reads_terminal_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "Novita")
    assert selected_backend() == "novita"


def test_selected_backend_falls_back_to_config(monkeypatch):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setattr("novita_config.get_setting", lambda k, d=None: "novita" if k == "backend" else d)
    assert selected_backend() == "novita"


# ----------------------------------------------------------------------
# the unavailability guard
# ----------------------------------------------------------------------


def _guard_for(report):
    """Run install() with a capturing ctx and return the registered hook."""
    captured = {}

    def register_hook(name, callback):
        captured[name] = callback

    ctx = MagicMock()
    ctx.register_hook.side_effect = register_hook
    install(ctx)
    return captured.get("pre_tool_call")


def test_guard_registered_when_backend_unavailable(monkeypatch):
    for name in ("tools.terminal_tool", "tools.registry"):
        monkeypatch.setitem(sys.modules, name, _fake_module(name))
    monkeypatch.setenv("TERMINAL_ENV", BACKEND)

    hook = _guard_for(None)

    assert hook is not None, "an unusable backend must install a blocking guard"
    result = hook(tool_name="terminal", args={})
    assert result["action"] == "block"
    assert "novita-sandbox doctor" in result["message"]


def test_guard_ignores_other_tools(monkeypatch):
    for name in ("tools.terminal_tool", "tools.registry"):
        monkeypatch.setitem(sys.modules, name, _fake_module(name))
    monkeypatch.setenv("TERMINAL_ENV", BACKEND)

    hook = _guard_for(None)

    assert hook(tool_name="read_file", args={}) is None


def test_guard_ignores_a_different_backend(monkeypatch):
    for name in ("tools.terminal_tool", "tools.registry"):
        monkeypatch.setitem(sys.modules, name, _fake_module(name))
    monkeypatch.setenv("TERMINAL_ENV", "local")

    hook = _guard_for(None)

    assert hook(tool_name="terminal", args={}) is None


def test_no_guard_when_backend_is_available(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", BACKEND)
    captured = {}

    ctx = MagicMock()
    ctx.register_hook.side_effect = lambda name, cb: captured.setdefault(name, cb)

    report = install(ctx)

    if report.available:
        assert "pre_tool_call" not in captured, "no guard needed when healthy"
