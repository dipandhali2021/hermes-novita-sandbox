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


def get_report():
    """The latest InjectionReport, or None if register() has not run."""
    return _report
