"""Managed patch: add Novita to `hermes setup`'s terminal backend menu.

Hermes exposes no extension point for this. The menu is built from
function-local literals inside ``setup_terminal_backend()``
(``hermes_cli/setup.py:1167``): ``terminal_choices``, ``idx_to_backend`` and
``backend_to_idx`` are locals, not module constants, and none of the 20
lifecycle hooks fires during setup. Platform plugins get a ``setup_fn``, but
that path is for gateway adapters, not terminal backends.

So the only way to add a menu row is to edit that file. This module does it in
the most conservative way available:

- exactly two insertions, each wrapped in unique markers;
- anchored on existing lines, and **refused** if an anchor is missing or
  ambiguous, or if the result would not parse;
- idempotent (a guard inside the inserted code and a marker check before writing);
- a backup is written once, and ``revert()`` removes the blocks by marker.

Because ``hermes update`` runs ``git reset --hard``, the edit is reverted by any
update. That is handled by *detection* rather than by silently re-editing core
files at runtime: once the patch has been applied we record it in a state file,
and if the markers later disappear we report it (log line + ``doctor``) so the
user can re-run ``hermes novita-sandbox patch-setup``.

No Hermes logic is duplicated: the inserted handler block is a normal
``elif selected_backend == "novita":`` arm, so the surrounding wizard keeps
working exactly as upstream wrote it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BEGIN = "    # --- novita-sandbox plugin: managed block (do not edit) ---"
END = "    # --- end novita-sandbox block ---"

MENU_ANCHOR = "    # Add keep current option\n"
HANDLER_ANCHOR = '    elif selected_backend == "ssh":\n'

STATE_FILE = ".setup-patch.json"
BACKUP_SUFFIX = ".novita-orig"
PLUGIN_KEY = "novita-sandbox"


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        import os

        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


PLUGIN_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# inserted code
# ----------------------------------------------------------------------

MENU_BLOCK = (
    BEGIN
    + "\n"
    + '''    if "novita" not in backend_to_idx:
        terminal_choices.append("Novita - Novita Agent Sandbox (cloud)")
        idx_to_backend[next_idx] = "novita"
        backend_to_idx["novita"] = next_idx
        next_idx += 1
'''
    + END
    + "\n"
)

# Deliberately a PLAIN string, not an f-string: the inserted code contains
# literal braces ({} and []) and f-strings of its own, which an f-string here
# would try to evaluate at import time in *this* module.
HANDLER_BLOCK = (
    BEGIN
    + "\n"
    + '''    elif selected_backend == "novita":
        print_success("Terminal backend: Novita")
        print_info("Novita Agent Sandbox - persistent cloud sandboxes.")
        print_info("Sign up at: https://novita.ai")

        try:
            __import__("novita_sandbox")
        except ImportError:
            print_info("Installing novita-sandbox SDK...")
            import subprocess as _nv_sp

            _nv_spec = "novita-sandbox>=2.1.0,<3"
            _nv_uv = shutil.which("uv")
            _nv_cmd = (
                [_nv_uv, "pip", "install", "--python", sys.executable, _nv_spec]
                if _nv_uv
                else [sys.executable, "-m", "pip", "install", _nv_spec]
            )
            _nv_res = _nv_sp.run(_nv_cmd, capture_output=True, text=True)
            if _nv_res.returncode == 0:
                print_success("novita-sandbox SDK installed")
            else:
                print_warning(
                    "Install failed - run: uv pip install --python "
                    + sys.executable
                    + " '" + _nv_spec + "'"
                )

        print()
        _nv_existing = get_env_value("NOVITA_API_KEY")
        if _nv_existing:
            print_info("  Novita API key: already configured")
            if prompt_yes_no("  Update API key?", False):
                _nv_key = prompt("    Novita API key", password=True)
                if _nv_key:
                    save_env_value("NOVITA_API_KEY", _nv_key)
                    print_success("    Updated")
        else:
            _nv_key = prompt("    Novita API key", password=True)
            if _nv_key:
                save_env_value("NOVITA_API_KEY", _nv_key)
                print_success("    Configured")
            else:
                print_warning("    No key entered - set NOVITA_API_KEY before use")

        config["terminal"].setdefault("novita_template", "base")
        config["terminal"].setdefault("novita_timeout", 3600)

        # The backend ships as a plugin; without this the selection is inert.
        _nv_enabled = config.setdefault("plugins", {}).setdefault("enabled", [])
        if isinstance(_nv_enabled, list) and "@@KEY@@" not in _nv_enabled:
            _nv_enabled.append("@@KEY@@")
            print_success("Enabled the novita-sandbox plugin")
'''
    + END
    + "\n"
).replace("@@KEY@@", PLUGIN_KEY)


# ----------------------------------------------------------------------
# semantic preconditions
# ----------------------------------------------------------------------

# Names the inserted blocks rely on. Checked before any write so that a Hermes
# release which renames a local (rather than moving an anchor) is refused
# instead of being injected with code that would raise NameError *inside*
# `hermes setup`. This is what makes automatic re-application safe.
REQUIRED_LOCALS = (
    "terminal_choices",
    "idx_to_backend",
    "backend_to_idx",
    "next_idx",
    "selected_backend",
)
REQUIRED_MODULE_NAMES = (
    "print_success",
    "print_info",
    "print_warning",
    "prompt",
    "prompt_yes_no",
    "get_env_value",
    "save_env_value",
)

FUNCTION_NAME = "setup_terminal_backend"


def semantic_precheck(text: str) -> tuple[bool, str]:
    """Confirm setup_terminal_backend() still offers what our blocks use."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return False, f"setup.py does not parse: {e}"

    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == FUNCTION_NAME
        ),
        None,
    )
    if function is None:
        return False, f"{FUNCTION_NAME}() not found"

    local_names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            local_names.add(node.id)
        elif isinstance(node, ast.arg):
            local_names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local_names.add(node.name)

    missing_locals = [name for name in REQUIRED_LOCALS if name not in local_names]
    if missing_locals:
        return False, (
            f"{FUNCTION_NAME}() no longer references: " + ", ".join(missing_locals)
        )

    module_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module_names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    module_names.add(target.id)

    missing_module = [n for n in REQUIRED_MODULE_NAMES if n not in module_names]
    if missing_module:
        return False, (
            "setup.py no longer provides at module level: " + ", ".join(missing_module)
        )

    return True, "ok"


# ----------------------------------------------------------------------
# state file (records that the user opted into the patch)
# ----------------------------------------------------------------------


def _state_path() -> Path:
    return PLUGIN_DIR / STATE_FILE


def read_state() -> dict:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(applied: bool) -> None:
    path = _state_path()
    try:
        if not applied:
            path.unlink(missing_ok=True)
            return
        state = read_state()
        state.update(
            {
                "applied": True,
                "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "target": str(setup_py_path() or ""),
            }
        )
        path.write_text(json.dumps(state, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.debug("novita: could not write setup-patch state: %s", e)


# ----------------------------------------------------------------------
# location
# ----------------------------------------------------------------------


def setup_py_path() -> Path | None:
    """Locate hermes_cli/setup.py without importing (and executing) it."""
    try:
        spec = importlib.util.find_spec("hermes_cli.setup")
    except Exception:  # noqa: BLE001
        return None
    origin = getattr(spec, "origin", None)
    if not origin:
        return None
    path = Path(origin)
    return path if path.is_file() else None


# ----------------------------------------------------------------------
# inspection
# ----------------------------------------------------------------------


def is_patched(text: str | None = None) -> bool:
    if text is None:
        path = setup_py_path()
        if path is None:
            return False
        try:
            text = path.read_text()
        except Exception:  # noqa: BLE001
            return False
    return BEGIN in text and END in text


def detect() -> dict:
    """Report the current state of the setup menu patch."""
    path = setup_py_path()
    if path is None:
        return {"state": "no_setup_module", "detail": "hermes_cli/setup.py not found"}

    try:
        text = path.read_text()
    except Exception as e:  # noqa: BLE001
        return {"state": "unreadable", "detail": f"{type(e).__name__}: {e}", "path": str(path)}

    if is_patched(text):
        return {"state": "patched", "path": str(path)}

    missing = []
    if text.count(MENU_ANCHOR) != 1:
        missing.append(f"menu anchor found {text.count(MENU_ANCHOR)}x (need exactly 1)")
    if text.count(HANDLER_ANCHOR) != 1:
        missing.append(f"handler anchor found {text.count(HANDLER_ANCHOR)}x (need exactly 1)")
    if missing:
        return {
            "state": "shape_unknown",
            "detail": "; ".join(missing),
            "path": str(path),
        }

    previously = bool(read_state().get("applied"))
    return {
        "state": "reverted" if previously else "unpatched",
        "detail": (
            "the patch was applied before and is now gone -- a Hermes update "
            "reverted it (git reset --hard)"
            if previously
            else "the patch has never been applied"
        ),
        "path": str(path),
    }


# ----------------------------------------------------------------------
# apply / revert
# ----------------------------------------------------------------------


def apply() -> tuple[bool, str]:
    """Insert the menu row and handler arm. Idempotent. Never leaves a broken file."""
    path = setup_py_path()
    if path is None:
        return False, "hermes_cli/setup.py not found; cannot patch"

    try:
        original = path.read_text()
    except Exception as e:  # noqa: BLE001
        return False, f"cannot read {path}: {e}"

    if is_patched(original):
        _write_state(True)
        return True, f"already patched ({path})"

    if original.count(MENU_ANCHOR) != 1:
        return False, (
            f"refusing to patch: the menu anchor appears "
            f"{original.count(MENU_ANCHOR)} times (expected exactly 1). "
            "Hermes' setup wizard has changed shape; the plugin needs updating."
        )
    if original.count(HANDLER_ANCHOR) != 1:
        return False, (
            f"refusing to patch: the handler anchor appears "
            f"{original.count(HANDLER_ANCHOR)} times (expected exactly 1). "
            "Hermes' setup wizard has changed shape; the plugin needs updating."
        )

    semantic_ok, semantic_detail = semantic_precheck(original)
    if not semantic_ok:
        return False, (
            f"refusing to patch: {semantic_detail}. The wizard still has the "
            "anchor lines but no longer defines what the inserted code uses, so "
            "injecting it would break `hermes setup`. The plugin needs updating."
        )

    patched = original.replace(MENU_ANCHOR, MENU_BLOCK + MENU_ANCHOR, 1)
    patched = patched.replace(HANDLER_ANCHOR, HANDLER_BLOCK + HANDLER_ANCHOR, 1)

    # Never write a file we cannot parse.
    try:
        ast.parse(patched)
    except SyntaxError as e:
        return False, f"refusing to patch: result would not parse ({e})"

    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    try:
        # Always refresh, so the backup records the content we actually patched
        # from. A stale one would be misleading after an update reverted the
        # file. (Repeat applies return early above, so this cannot clobber.)
        shutil.copy2(path, backup)
        path.write_text(patched)
    except Exception as e:  # noqa: BLE001
        return False, f"cannot write {path}: {e}"

    # Verify by re-reading.
    try:
        written = path.read_text()
        ast.parse(written)
        if not is_patched(written):
            return False, "write appeared to succeed but the markers are absent"
    except Exception as e:  # noqa: BLE001
        return False, f"verification failed after write: {e}"

    _write_state(True)
    return True, f"patched {path} (backup: {backup.name})"


def revert() -> tuple[bool, str]:
    """Remove the inserted blocks, restoring the file's original shape."""
    path = setup_py_path()
    if path is None:
        return False, "hermes_cli/setup.py not found"

    try:
        text = path.read_text()
    except Exception as e:  # noqa: BLE001
        return False, f"cannot read {path}: {e}"

    if not is_patched(text):
        _write_state(False)
        return True, "nothing to revert (markers absent)"

    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == BEGIN:
            skipping = True
            continue
        if stripped == END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    reverted = "".join(out)

    try:
        ast.parse(reverted)
    except SyntaxError as e:
        return False, f"refusing to revert: result would not parse ({e})"

    try:
        path.write_text(reverted)
    except Exception as e:  # noqa: BLE001
        return False, f"cannot write {path}: {e}"

    _write_state(False)
    return True, f"reverted {path}"


def status_line() -> str:
    """One-line summary for `doctor`."""
    info = detect()
    state = info["state"]
    if state == "patched":
        return "[ok]   setup menu: Novita row present"
    if state == "reverted":
        return ("[FAIL] setup menu: patch was reverted (likely by `hermes update`) "
                "-- run `hermes novita-sandbox patch-setup`")
    if state == "unpatched":
        return ("[skip] setup menu: Novita row not added "
                "-- run `hermes novita-sandbox patch-setup` to add it")
    if state == "shape_unknown":
        return f"[FAIL] setup menu: cannot patch -- {info.get('detail')}"
    return f"[FAIL] setup menu: {state} -- {info.get('detail', '')}"


# ----------------------------------------------------------------------
# automatic re-application after an update
# ----------------------------------------------------------------------


def auto_reapply_enabled() -> bool:
    """Whether to re-apply the menu patch automatically after an update.

    Default **on** -- but it only ever acts when the state file records a prior
    apply, so a patch the user never chose is never applied for them. Turn it
    off with ``terminal.novita_auto_patch_setup: false`` in config.yaml, or by
    running ``unpatch-setup``, which clears the state file.
    """
    try:
        from . import config as _config

        return _config.get_bool_setting("novita_auto_patch_setup", True)
    except Exception:  # noqa: BLE001
        return True


def ensure_applied() -> dict:
    """Re-apply the menu patch if an update reverted it. Never raises.

    Called from the plugin's ``register()`` on every Hermes start. It does
    nothing unless the state file shows the user previously applied the patch
    *and* the markers are now absent -- i.e. an update ran ``git reset --hard``.

    Re-application goes through the same guarded ``apply()``: anchors must be
    unique, the wizard must still define the names the inserted code uses, and
    the result must parse. If any of that fails it reports and stops rather than
    writing code that could break ``hermes setup``.
    """
    try:
        info = detect()
        state = info.get("state")

        if state == "patched":
            return {"action": "none", "state": state}

        if state == "shape_unknown":
            # The row is gone and we cannot put it back. If the user had opted
            # in, that is worth telling them about rather than passing over in
            # silence -- otherwise the menu simply loses Novita with no signal.
            if read_state().get("applied"):
                logger.warning(
                    "novita-sandbox: the Novita row is missing from `hermes setup` "
                    "and cannot be re-added because the wizard changed shape (%s). "
                    "The Novita backend is unaffected. Update the plugin to restore "
                    "the row.",
                    info.get("detail", ""),
                )
                return {
                    "action": "needs_plugin_update",
                    "state": state,
                    "detail": info.get("detail", ""),
                }
            return {"action": "none", "state": state}

        if state != "reverted":
            # Never apply a patch the user did not ask for, and never touch a
            # file whose shape cannot be verified.
            return {"action": "none", "state": state}

        if not auto_reapply_enabled():
            return {
                "action": "skipped",
                "state": state,
                "reason": "disabled by terminal.novita_auto_patch_setup",
            }

        ok, message = apply()
        if ok:
            logger.info(
                "novita-sandbox: re-applied the `hermes setup` menu patch after a "
                "Hermes update (%s)",
                message,
            )
            return {"action": "applied", "state": state, "detail": message}

        logger.warning(
            "novita-sandbox: could not re-apply the `hermes setup` menu patch: %s. "
            "The Novita backend is unaffected; run `hermes novita-sandbox doctor`.",
            message,
        )
        return {"action": "failed", "state": state, "detail": message}
    except Exception as e:  # noqa: BLE001 - must never break plugin discovery
        logger.debug("novita-sandbox: auto re-apply check failed: %s", e)
        return {"action": "error", "state": "unknown", "detail": str(e)}
