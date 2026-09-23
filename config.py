"""Configuration and credential resolution for the Novita backend.

Resolution order for every setting: explicit argument > ``terminal.*`` in
config.yaml > the plugin's own defaults.  The plugin never depends on Hermes'
config merge knowing about its keys, so a future loader change that drops
unknown keys cannot break it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Defaults mirror Hermes' own terminal defaults where they overlap.
DEFAULTS: dict[str, Any] = {
    "novita_template": "base",
    # Sandbox lifetime in seconds. Note: this is measured from sandbox CREATION,
    # not from each call -- see the design spec section 12.6.
    "novita_timeout": 3600,
    # How far ahead of "now" the keep-alive bump aims, per execute.
    "novita_refresh_window": 3600,
    "container_persistent": True,
    "container_cpu": 2,
    "container_memory": 4096,
    # Re-add the `hermes setup` menu row automatically after a Hermes update
    # reverts it. Only ever acts if the patch was applied before, so it is opt-in
    # by construction; set false to stop that.
    "novita_auto_patch_setup": True,
}

TRUEISH = {"1", "true", "yes", "on"}


def hermes_home() -> Path:
    """Profile-aware HERMES_HOME, per the AGENTS.md profile-safety rule."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        return Path(env) if env else Path.home() / ".hermes"


def sdk_available() -> bool:
    """Whether novita_sandbox is importable.

    Uses ``find_spec`` rather than importing, so a presence check has no import
    side effects and no unused-import noise. Single source of truth: the
    environment, the CLI, and the injector all ask this.
    """
    try:
        import importlib.util

        return importlib.util.find_spec("novita_sandbox") is not None
    except (ImportError, ValueError):
        return False


def _read_yaml_terminal() -> dict[str, Any]:
    """Fallback: read the ``terminal:`` section of config.yaml directly."""
    cfg_path = hermes_home() / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(cfg_path.read_text(errors="replace")) or {}
        section = data.get("terminal")
        return section if isinstance(section, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.debug("novita: could not read config.yaml directly: %s", e)
        return {}


def terminal_config() -> dict[str, Any]:
    """The ``terminal:`` config section, via Hermes' loader when available."""
    try:
        from hermes_cli.config import load_config  # type: ignore

        section = (load_config() or {}).get("terminal")
        if isinstance(section, dict):
            return section
    except Exception as e:  # noqa: BLE001
        # Not fatal: the direct read below covers the common case, and the
        # caller falls back to DEFAULTS. A plugin must never raise here.
        logger.debug("novita: hermes config loader unavailable (%s); reading YAML", e)
    return _read_yaml_terminal()


def get_setting(key: str, default: Any = None) -> Any:
    """Resolve one setting: config.yaml > plugin default > *default*."""
    cfg = terminal_config()
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default


def get_bool_setting(key: str, default: bool = False) -> bool:
    value = get_setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUEISH


def get_int_setting(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def _read_env_file() -> dict[str, str]:
    """Parse ``$HERMES_HOME/.env`` (Hermes' secrets-only file)."""
    out: dict[str, str] = {}
    env_path = hermes_home() / ".env"
    if not env_path.is_file():
        return out
    try:
        for line in env_path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as e:  # noqa: BLE001
        logger.debug("novita: could not read .env: %s", e)
    return out


def get_api_key() -> str | None:
    """Resolve NOVITA_API_KEY: process env first, then $HERMES_HOME/.env."""
    key = os.environ.get("NOVITA_API_KEY")
    if key and key.strip():
        return key.strip()
    key = _read_env_file().get("NOVITA_API_KEY")
    return key.strip() if key and key.strip() else None


def export_api_key() -> str | None:
    """Ensure NOVITA_API_KEY is in ``os.environ``, and return it.

    Required: the Novita SDK's *class-level* calls (``Sandbox.list``,
    ``Sandbox.connect``) resolve credentials from the process environment, not
    from a client instance. Passing the key only to ``Novita(...)`` leaves those
    calls raising ``AuthenticationException`` -- a silent production failure
    found by live probe (design spec section 12.7).
    """
    key = get_api_key()
    if key and not os.environ.get("NOVITA_API_KEY"):
        os.environ["NOVITA_API_KEY"] = key
    return key


def describe() -> dict[str, Any]:
    """Non-secret config summary, for ``hermes novita-sandbox doctor``."""
    return {
        "novita_template": get_setting("novita_template"),
        "novita_timeout": get_int_setting("novita_timeout", 3600),
        "novita_refresh_window": get_int_setting("novita_refresh_window", 3600),
        "container_persistent": get_bool_setting("container_persistent", True),
        "container_cpu": get_int_setting("container_cpu", 2),
        "container_memory": get_int_setting("container_memory", 4096),
        "api_key_present": bool(get_api_key()),
        "hermes_home": str(hermes_home()),
    }
