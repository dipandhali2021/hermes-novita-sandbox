"""Runtime injection of the Novita backend into Hermes' terminal path.

This is the update-proofing layer.  **It writes no files and edits no core
source.**  It only rebinds attributes inside already-imported modules, and every
patch is individually capability-probed so that a future Hermes release which
renames or removes an internal makes exactly one patch report a failure instead
of breaking the install.

Nothing here may raise: ``install()`` is called from ``register()`` during plugin
discovery, and a plugin that explodes during discovery is worse than one that
does nothing.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

BACKEND = "novita"

# Patches whose failure means the backend cannot work at all.
PIVOTAL = frozenset({"factory", "check_fn"})

LAZY_DEP_SPEC = ("novita-sandbox>=2.1.0,<3",)


def _module(dotted: str) -> Any:
    """Resolve a module by dotted name, deterministically.

    Deliberately ``importlib.import_module`` rather than
    ``import a.b as name``: the latter binds via the *parent package
    attribute*, which can disagree with ``sys.modules`` (e.g. a module that was
    replaced at runtime). Resolving through ``sys.modules`` is unambiguous and
    keeps the probes testable against synthetic modules.
    """
    return importlib.import_module(dotted)


@dataclass
class PatchResult:
    name: str
    status: str  # applied | already | skipped | failed
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"applied", "already"}


@dataclass
class InjectionReport:
    hermes_version: str = "unknown"
    patches: list[PatchResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.patches.append(PatchResult(name, status, detail))

    def get(self, name: str) -> PatchResult | None:
        for patch in self.patches:
            if patch.name == name:
                return patch
        return None

    @property
    def available(self) -> bool:
        """True when the backend can actually serve commands."""
        return all((self.get(name) or PatchResult(name, "failed")).ok for name in PIVOTAL)

    def degradations(self) -> list[PatchResult]:
        return [p for p in self.patches if not p.ok and p.name not in PIVOTAL]

    def format_text(self) -> str:
        lines = [f"hermes {self.hermes_version}  ->  novita backend "
                 f"{'AVAILABLE' if self.available else 'UNAVAILABLE'}"]
        for patch in self.patches:
            lines.append(f"  [{patch.status:>7}] {patch.name}"
                         + (f" -- {patch.detail}" if patch.detail else ""))
        if not self.available:
            lines.append("  The backend is disabled; Hermes falls back to its "
                         "configured behaviour.")
        return "\n".join(lines)


def hermes_version() -> str:
    for getter in (
        lambda: __import__("importlib.metadata", fromlist=["version"]).version("hermes-agent"),
    ):
        try:
            value = getter()
            if value:
                return str(value)
        except Exception:  # noqa: BLE001
            pass
    try:
        from hermes_cli.build_info import get_build_info  # type: ignore

        info = get_build_info() or {}
        return str(info.get("version") or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def selected_backend() -> str:
    """The configured terminal backend name, lowercased."""
    try:
        env = os.environ.get("TERMINAL_ENV")
        if env:
            return env.strip().lower()
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import config as _config

        return str(_config.get_setting("backend", "local") or "local").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------------
# Individual patches
# ----------------------------------------------------------------------


def _patch_factory(report: InjectionReport) -> None:
    """Route ``_create_environment(env_type="novita")`` to NovitaEnvironment.

    Reliable because every consumer imports the factory *inside a function*, so
    the name is re-resolved from the module on each call -- verified at
    tools/terminal_tool.py:2036 (bare global), tools/code_execution_tool.py:608
    and tools/file_tools.py:745 (function-local imports).
    """
    try:
        terminal_tool = _module("tools.terminal_tool")
    except Exception as e:  # noqa: BLE001
        report.add("factory", "failed", f"cannot import tools.terminal_tool: {e}")
        return

    original = getattr(terminal_tool, "_create_environment", None)
    if not callable(original):
        report.add("factory", "failed", "tools.terminal_tool._create_environment not found")
        return
    if getattr(original, "__novita_wrapped__", False):
        report.add("factory", "already")
        return

    from . import config as _config

    def wrapped(*args: Any, **kwargs: Any):
        try:
            env_type = kwargs.get("env_type")
            if env_type is None and args:
                env_type = args[0]
            if str(env_type or "").strip().lower() == BACKEND:
                from .environment import NovitaEnvironment

                cwd = kwargs.get("cwd")
                if cwd is None and len(args) > 2:
                    cwd = args[2]
                timeout = kwargs.get("timeout")
                if timeout is None and len(args) > 3:
                    timeout = args[3]
                task_id = kwargs.get("task_id")
                if task_id is None:
                    task_id = args[7] if len(args) > 7 else "default"

                return NovitaEnvironment(
                    template=_config.get_setting("novita_template", "base"),
                    cwd=cwd or "/root",
                    timeout=int(timeout or 60),
                    persistent_filesystem=_config.get_bool_setting(
                        "container_persistent", True
                    ),
                    task_id=str(task_id or "default"),
                )
        except Exception:  # noqa: BLE001
            # Fall through to the built-in ladder rather than break the call.
            logger.exception("novita: factory interception failed; delegating")

        return original(*args, **kwargs)

    wrapped.__novita_wrapped__ = True
    wrapped.__novita_original__ = original
    wrapped.__doc__ = getattr(original, "__doc__", None)
    terminal_tool._create_environment = wrapped
    report.add("factory", "applied")


def _novita_reachable() -> bool:
    try:
        from . import config as _config

        if not _config.get_api_key():
            return False
        import novita_sandbox  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _patch_check_fn(report: InjectionReport) -> None:
    """Teach the terminal tool's availability check about ``novita``.

    ``check_terminal_requirements`` is bound as ``check_fn`` at
    ``registry.register(...)`` time (tools/terminal_tool.py:2743), so rebinding
    the module attribute is not enough -- the registry entry must be updated.
    ``_check_fn_cached`` keys its cache by the function object, so a new callable
    is never served a stale result; ``invalidate_check_fn_cache()`` is belt-and-
    braces.
    """
    entry = None
    invalidate: Callable[[], None] | None = None
    try:
        from tools.registry import invalidate_check_fn_cache, registry

        entry = registry.get_entry("terminal")
        invalidate = invalidate_check_fn_cache
    except Exception as e:  # noqa: BLE001
        report.add("check_fn", "failed", f"cannot reach the tool registry: {e}")
        return

    if entry is None:
        report.add("check_fn", "failed", "no 'terminal' tool registered")
        return

    original = getattr(entry, "check_fn", None)
    if getattr(original, "__novita_wrapped__", False):
        report.add("check_fn", "already")
        return
    if original is not None and not callable(original):
        report.add("check_fn", "failed", f"check_fn is {type(original).__name__}, not callable")
        return

    def check() -> bool:
        try:
            if selected_backend() == BACKEND:
                # We own the verdict for novita; the built-in check would
                # reject the unknown name with "Unknown TERMINAL_ENV".
                return _novita_reachable()
        except Exception:  # noqa: BLE001
            pass
        if original is None:
            return True
        return bool(original())

    check.__novita_wrapped__ = True
    entry.check_fn = check

    # Also rebind the module-level function, which tools/__init__.py:20-22 and
    # tools/terminal_tool.py:2642 call by name.
    try:
        terminal_tool = _module("tools.terminal_tool")

        module_fn = getattr(terminal_tool, "check_terminal_requirements", None)
        if callable(module_fn) and not getattr(module_fn, "__novita_wrapped__", False):

            def module_check() -> bool:
                if selected_backend() == BACKEND:
                    return _novita_reachable()
                return bool(module_fn())

            module_check.__novita_wrapped__ = True
            terminal_tool.check_terminal_requirements = module_check
    except Exception as e:  # noqa: BLE001
        logger.debug("novita: module-level check_fn rebind skipped: %s", e)

    if invalidate is not None:
        try:
            invalidate()
        except Exception as e:  # noqa: BLE001
            logger.debug("novita: could not invalidate check_fn cache: %s", e)

    report.add("check_fn", "applied")


def _add_to_name_set(
    report: InjectionReport,
    name: str,
    module_name: str,
    attr: str,
) -> None:
    """Add BACKEND to a module-level frozenset/set of backend names."""
    try:
        module = _module(module_name)
    except Exception as e:  # noqa: BLE001
        report.add(name, "failed", f"cannot import {module_name}: {e}")
        return

    current = getattr(module, attr, None)
    if current is None:
        report.add(name, "failed", f"{module_name}.{attr} not found")
        return
    if not isinstance(current, (set, frozenset)):
        report.add(
            name, "failed",
            f"{module_name}.{attr} is {type(current).__name__}, expected a set",
        )
        return
    if BACKEND in current:
        report.add(name, "already")
        return

    new_value = frozenset(current) | {BACKEND} if isinstance(current, frozenset) \
        else set(current) | {BACKEND}
    setattr(module, attr, new_value)
    report.add(name, "applied")


def _patch_descriptions(report: InjectionReport) -> None:
    """Register the per-backend fallback description used in the system prompt."""
    try:
        pb = _module("agent.prompt_builder")
    except Exception as e:  # noqa: BLE001
        report.add("descriptions", "failed", f"cannot import agent.prompt_builder: {e}")
        return

    table = getattr(pb, "_BACKEND_FALLBACK_DESCRIPTIONS", None)
    if not isinstance(table, dict):
        report.add(
            "descriptions", "failed",
            "_BACKEND_FALLBACK_DESCRIPTIONS is "
            f"{type(table).__name__}, expected a dict",
        )
        return

    if table.get(BACKEND) == "a Novita sandbox (Linux)":
        report.add("descriptions", "already")
        return
    table[BACKEND] = "a Novita sandbox (Linux)"
    report.add("descriptions", "applied")


def _patch_lazy_deps(report: InjectionReport) -> None:
    """Register a terminal.novita dependency group with Hermes' lazy installer."""
    try:
        lazy_deps = _module("tools.lazy_deps")
    except Exception as e:  # noqa: BLE001
        report.add("lazy_deps", "failed", f"cannot import tools.lazy_deps: {e}")
        return

    table = getattr(lazy_deps, "LAZY_DEPS", None)
    if not isinstance(table, dict):
        report.add("lazy_deps", "failed", f"LAZY_DEPS is {type(table).__name__}, expected a dict")
        return

    if table.get("terminal.novita"):
        report.add("lazy_deps", "already")
        return

    # Validate against Hermes' own allowlist so an install attempt cannot be
    # rejected later for a malformed spec.
    validator = getattr(lazy_deps, "_is_safe_spec", None)
    if callable(validator):
        for spec in LAZY_DEP_SPEC:
            try:
                if not validator(spec):
                    report.add("lazy_deps", "failed", f"spec rejected by Hermes allowlist: {spec}")
                    return
            except Exception:  # noqa: BLE001
                break

    table["terminal.novita"] = LAZY_DEP_SPEC
    report.add("lazy_deps", "applied")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def install(ctx: Any | None = None) -> InjectionReport:
    """Apply every patch, recording an outcome for each. Never raises."""
    report = InjectionReport(hermes_version=hermes_version())

    for step in (
        _patch_factory,
        _patch_check_fn,
        lambda r: _add_to_name_set(
            r, "prompt_remote_set", "agent.prompt_builder", "_REMOTE_TERMINAL_BACKENDS"
        ),
        _patch_descriptions,
        lambda r: _add_to_name_set(r, "env_probe_set", "tools.env_probe", "_REMOTE_BACKENDS"),
        lambda r: _add_to_name_set(
            r, "skills_remote_set", "tools.skills_tool", "_REMOTE_ENV_BACKENDS"
        ),
        _patch_lazy_deps,
    ):
        try:
            step(report)
        except Exception as e:  # noqa: BLE001 - one bad patch must not stop the rest
            label = getattr(step, "__name__", "patch")
            logger.warning("novita: patch %s raised: %s", label, e)
            report.add(label, "failed", f"raised {type(e).__name__}: {e}")

    logger.info("novita: injection report\n%s", report.format_text())

    if ctx is not None:
        _install_guard(ctx, report)
    return report


def _install_guard(ctx: Any, report: InjectionReport) -> None:
    """Block ``terminal`` calls with a clear message when the backend is unusable.

    Without this, a Hermes update that breaks a pivotal patch would surface as
    the confusing "Unknown TERMINAL_ENV: novita. Use 'local', 'docker', ..."
    from tools/terminal_tool.py:1365-1372.
    """
    if report.available:
        return

    failed = [p for p in report.patches if not p.ok and p.name in PIVOTAL]
    detail = "; ".join(f"{p.name}: {p.detail or 'failed'}" for p in failed) or "unknown probe failure"

    def on_pre_tool_call(**kwargs: Any):
        try:
            if str(kwargs.get("tool_name") or "") != "terminal":
                return None
            if selected_backend() != BACKEND:
                return None
            return {
                "action": "block",
                "message": (
                    f"novita backend unavailable on Hermes {report.hermes_version}: {detail}. "
                    "Run `hermes novita-sandbox doctor` for the full probe report, "
                    "or set terminal.backend to another backend (e.g. local)."
                ),
            }
        except Exception:  # noqa: BLE001
            return None

    try:
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
        logger.warning(
            "novita: backend unavailable; registered a guard that blocks terminal "
            "calls while terminal.backend is 'novita'."
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("novita: could not register the unavailability guard: %s", e)
