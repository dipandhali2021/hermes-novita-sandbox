"""Novita Agent Sandbox terminal backend for Hermes.

Adds ``novita`` as a terminal execution backend **without modifying any Hermes
source file**, so ``hermes update`` (``git fetch --depth 1`` + ``git reset
--hard``) cannot revert it.

Layout:

- ``environment.py`` -- ``NovitaEnvironment``, a plain ``BaseEnvironment``
  subclass. Knows nothing about Hermes internals; performs no patching.
- ``inject.py``      -- routes ``backend: novita`` to the above via 7
  individually capability-probed runtime patches.
- ``config.py``      -- ``terminal.*`` and ``NOVITA_API_KEY`` resolution.
- ``cli.py``         -- ``hermes novita-sandbox setup|doctor|status|install-template``.

Nothing here may raise: ``register()`` runs during plugin discovery, and a
plugin that explodes during discovery is worse than one that does nothing.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

# Populated by register(); read by the CLI's doctor command.
_report = None


def register(ctx) -> None:
    """Hermes plugin entry point. Never raises."""
    global _report

    try:
        from . import inject

        _report = inject.install(ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "novita-sandbox: injection failed (%s); the backend is unavailable "
            "but Hermes is unaffected.",
            e,
            exc_info=True,
        )

    try:
        from . import cli

        cli.register_cli(ctx)
    except Exception as e:  # noqa: BLE001
        logger.warning("novita-sandbox: CLI registration failed: %s", e, exc_info=True)

    # The `hermes setup` menu row is a file edit, so `hermes update` reverts it
    # (git reset --hard). Re-apply it automatically -- but only if the user
    # opted in by applying it once, and only through setup_patch's guards
    # (unique anchors, semantic preconditions, AST validation). If Hermes has
    # changed shape it reports and stops rather than writing code into a wizard
    # it no longer understands.
    try:
        from . import setup_patch

        outcome = setup_patch.ensure_applied()
        if outcome.get("action") in {"failed", "needs_plugin_update"}:
            logger.warning(
                "novita-sandbox: the Novita row is missing from the `hermes setup` "
                "menu and could not be re-added (%s). The backend still works. "
                "See `hermes novita-sandbox doctor`.",
                outcome.get("detail", ""),
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("novita-sandbox: setup-patch check skipped: %s", e)


def get_report():
    """The latest InjectionReport, or None if register() has not run."""
    return _report
