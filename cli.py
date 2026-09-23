"""CLI surface: ``hermes novita-sandbox <command>``.

Registered through Hermes' *sanctioned* ``ctx.register_cli_command`` surface, so
this survives ``hermes update`` (which runs ``git reset --hard`` over the source
tree).  It exists to provide the setup experience Hermes' own interactive
backend menu would otherwise provide: selecting the backend and entering the API
key, Daytona-style -- but without editing ``hermes_cli/setup.py``, which any
update would revert.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
from typing import Any

from . import config as _config
from . import inject

DEFAULT_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"
DEFAULT_TEMPLATE_NAME = "hermes-novita"


# ----------------------------------------------------------------------
# small helpers (each degrades rather than raising)
# ----------------------------------------------------------------------


def _tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _say(message: str) -> None:
    print(message)


def _ask(prompt: str, default: str = "") -> str:
    if not _tty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        reply = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return reply or default


def _ask_secret(prompt: str) -> str:
    """Masked prompt, preferring Hermes' own masked input helper."""
    try:
        from hermes_cli.secret_prompt import masked_secret_prompt  # type: ignore

        return masked_secret_prompt(prompt).strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        if _tty():
            return getpass.getpass(f"{prompt}: ").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _confirm(prompt: str, default: bool = False) -> bool:
    if not _tty():
        return default
    reply = _ask(f"{prompt} [{'Y/n' if default else 'y/N'}]")
    if not reply:
        return default
    return reply.strip().lower() in {"y", "yes"}


def _health_icon(ok: bool) -> str:
    return "[ok]  " if ok else "[FAIL]"


# ----------------------------------------------------------------------
# Hermes-side writers
# ----------------------------------------------------------------------


def _save_api_key(value: str) -> bool:
    try:
        from hermes_cli.config import save_env_value_secure  # type: ignore

        save_env_value_secure("NOVITA_API_KEY", value)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from hermes_cli.config import save_env_value  # type: ignore

        save_env_value("NOVITA_API_KEY", value)
        return True
    except Exception as e:  # noqa: BLE001
        _say(f"  Could not use Hermes' env writer ({e}); writing .env directly.")
    try:
        env_path = _config.hermes_home() / ".env"
        lines = []
        if env_path.is_file():
            lines = [
                ln for ln in env_path.read_text().splitlines()
                if not ln.strip().startswith("NOVITA_API_KEY=")
            ]
        lines.append(f"NOVITA_API_KEY={value}")
        env_path.write_text("\n".join(lines) + "\n")
        env_path.chmod(0o600)
        return True
    except Exception as e:  # noqa: BLE001
        _say(f"  FAILED to write {_config.hermes_home()}/.env: {e}")
        return False


def _set_config(key: str, value: Any) -> bool:
    try:
        from hermes_cli.config import set_config_value  # type: ignore

        set_config_value(key, str(value))
        return True
    except Exception as e:  # noqa: BLE001
        _say(f"  FAILED to set {key}: {e}")
        return False


# ----------------------------------------------------------------------
# SDK install
# ----------------------------------------------------------------------


def _find_uv() -> str | None:
    try:
        from hermes_cli.managed_uv import resolve_uv  # type: ignore

        path = resolve_uv()
        if path:
            return str(path)
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("uv")


def _install_sdk() -> bool:
    uv = _find_uv()
    if not uv:
        _say("  Could not find `uv`. Install the SDK manually, then re-run:")
        _say(f"    pip install --python {sys.executable} 'novita-sandbox>=2.1.0,<3'")
        return False
    cmd = [uv, "pip", "install", "--python", sys.executable, "novita-sandbox>=2.1.0,<3"]
    _say(f"  running: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:  # noqa: BLE001
        _say(f"  install failed: {e}")
        return False
    if result.returncode != 0:
        _say(f"  install failed (exit {result.returncode}):")
        _say((result.stderr or result.stdout or "").strip()[-1500:])
        return False
    _say("  installed.")
    return True


def _sdk_importable() -> bool:
    try:
        import novita_sandbox  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# Live validation
# ----------------------------------------------------------------------


def _validate_key(api_key: str) -> tuple[bool, str]:
    """Create a throwaway sandbox, run a command, kill it."""
    import os

    previous = os.environ.get("NOVITA_API_KEY")
    os.environ["NOVITA_API_KEY"] = api_key
    sandbox = None
    try:
        from novita_sandbox import Novita

        client = Novita(api_key=api_key)
        sandbox = client.sandbox.create("base", timeout=120, metadata={"hermes_setup": "1"})
        result = sandbox.commands.run("echo novita-ok && whoami && python3 --version")
        out = (result.stdout or "").strip()
        if "novita-ok" not in out:
            return False, f"unexpected output: {out[:200]!r}"
        return True, out.replace("\n", " | ")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
            except Exception:  # noqa: BLE001
                pass
        if previous is None:
            os.environ.pop("NOVITA_API_KEY", None)
        else:
            os.environ["NOVITA_API_KEY"] = previous


def _list_templates() -> list[dict]:
    key = _config.export_api_key()
    if not key:
        return []
    try:
        from novita_sandbox import Novita

        client = Novita(api_key=key)
        page = client.template.list(template_type="template_build", page=1, limit=50)
        items = getattr(page, "items", None) or getattr(page, "templates", None) or []
        out = []
        for item in items:
            out.append(
                {
                    "template_id": getattr(item, "template_id", "?"),
                    "names": getattr(item, "names", None) or getattr(item, "aliases", []) or [],
                    "cpu": getattr(item, "cpu_count", "?"),
                    "memory": getattr(item, "memory_mb", "?"),
                }
            )
        return out
    except Exception as e:  # noqa: BLE001
        _say(f"  could not list templates: {e}")
        return []


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------


def cmd_doctor(args: Any = None) -> int:
    _say("")
    _say("Novita sandbox backend -- doctor")
    _say("=" * 60)

    report = inject.install(None)
    _say(report.format_text())

    _say("")
    _say("Configuration")
    for key, value in _config.describe().items():
        _say(f"  {key}: {value}")

    _say("")
    _say("Checks")
    _say(f"  {_health_icon(True)} plugin loaded")
    _say(f"  {_health_icon(_sdk_importable())} novita-sandbox importable")
    key_present = bool(_config.get_api_key())
    _say(f"  {_health_icon(key_present)} NOVITA_API_KEY set")
    _say(f"  {_health_icon(bool(_find_uv()))} uv available (for installs)")

    try:
        from . import setup_patch

        _say("")
        _say("Setup menu integration")
        _say(f"  {setup_patch.status_line()}")
    except Exception as e:  # noqa: BLE001
        _say(f"  setup menu: check unavailable ({e})")

    if not report.available:
        _say("")
        _say("The backend is DISABLED because a pivotal probe failed. Hermes will")
        _say("keep working; set terminal.backend to another value to silence the guard.")
        return 1

    if key_present and _sdk_importable() and getattr(args, "live", False):
        _say("")
        _say("Live check")
        ok, detail = _validate_key(_config.get_api_key() or "")
        _say(f"  {_health_icon(ok)} sandbox create/exec {'succeeded' if ok else 'failed'}")
        _say(f"      {detail}")
        return 0 if ok else 1

    if report.degradations():
        _say("")
        _say("Non-fatal degradations (backend still works):")
        for patch in report.degradations():
            _say(f"  - {patch.name}: {patch.detail or patch.status}")
    _say("")
    _say("Run with --live to create a real sandbox and verify end to end.")
    return 0


def cmd_status(args: Any = None) -> int:
    _say("")
    _say("Novita sandbox backend -- status")
    _say("=" * 60)
    for key, value in _config.describe().items():
        _say(f"  {key}: {value}")

    key = _config.export_api_key()
    if not key:
        _say("\nNo API key; nothing further to report. Run: hermes novita-sandbox setup")
        return 0

    _say("\nSandboxes on this account:")
    try:
        from novita_sandbox import Novita, SandboxQuery, SandboxState

        client = Novita(api_key=key)
        paginator = client.sandbox.list(
            query=SandboxQuery(state=[SandboxState.RUNNING, SandboxState.PAUSED])
        )
        from .environment import iter_paginator

        count = 0
        for info in iter_paginator(paginator):
            metadata = getattr(info, "metadata", None) or {}
            task = metadata.get("hermes_task_id", "-")
            _say(f"  {info.sandbox_id}  {info.state}  task={task}")
            count += 1
        if count == 0:
            _say("  (none)")
    except Exception as e:  # noqa: BLE001
        _say(f"  FAILED: {type(e).__name__}: {e}")
        return 1
    return 0


def cmd_install_template(args: Any) -> int:
    image = getattr(args, "image", DEFAULT_IMAGE)
    name = getattr(args, "name", DEFAULT_TEMPLATE_NAME)
    cpu = int(getattr(args, "cpu", 2))
    memory = int(getattr(args, "memory", 4096))

    if not _config.export_api_key():
        _say("NOVITA_API_KEY is not set. Run: hermes novita-sandbox setup")
        return 1
    if not _sdk_importable():
        _say("novita-sandbox is not installed. Run: hermes novita-sandbox setup")
        return 1

    _say("")
    _say(f"Building template '{name}' from {image}")
    _say(f"  cpu={cpu} memory={memory}MB")
    _say("  This runs once and can take several minutes.")
    _say("")
    try:
        from novita_sandbox import Template

        template = Template().from_image(image)
        build = Template.build(template, name, cpu_count=cpu, memory_mb=memory)
    except Exception as e:  # noqa: BLE001
        _say(f"  BUILD FAILED: {type(e).__name__}: {e}")
        _say("")
        _say("  If this is a quota/permission error, your account may not permit")
        _say("  template builds. Fall back to the system template:")
        _say("    hermes config set terminal.novita_template base")
        return 1

    template_id = getattr(build, "template_id", None) or getattr(build, "templateId", None)
    _say(f"  built: template_id={template_id}")
    if template_id and _set_config("terminal.novita_template", template_id):
        _say(f"  saved terminal.novita_template = {template_id}")
    else:
        _say(f"  Set this manually:  terminal.novita_template: {template_id}")
    return 0


def cmd_setup(args: Any) -> int:
    """Interactive setup: SDK, API key, template, backend selection, verify."""
    _say("")
    _say("Novita sandbox backend -- setup")
    _say("=" * 60)
    _say("This configures Hermes to run terminal commands inside Novita")
    _say("sandboxes. Nothing here modifies Hermes source files.")
    _say("")

    assume_yes = bool(getattr(args, "yes", False))

    # 1. SDK -----------------------------------------------------------
    _say("[1/5] Novita SDK")
    if _sdk_importable():
        _say(f"  {_health_icon(True)} already installed")
    else:
        _say(f"  {_health_icon(False)} not installed")
        if assume_yes or _confirm("  Install novita-sandbox now?", True):
            if not _install_sdk():
                return 1
        else:
            _say("  Skipped. Re-run setup once it is installed.")
            return 1

    # 2. API key -------------------------------------------------------
    _say("")
    _say("[2/5] API key")
    existing = _config.get_api_key()
    api_key = getattr(args, "api_key", None)

    if api_key:
        _say("  using the key passed on the command line")
    elif existing:
        _say("  a key is already configured")
        if assume_yes or _confirm("  Replace it?", False):
            api_key = _ask_secret("  Paste the new NOVITA_API_KEY")
        else:
            api_key = None
    else:
        _say("  Get one at https://novita.ai/dashboard (Team tab)")
        api_key = _ask_secret("  Paste your NOVITA_API_KEY")

    if api_key:
        api_key = api_key.strip()
        if not api_key:
            _say("  No key entered.")
            return 1
        _say("  validating against the live API (creates a throwaway sandbox)...")
        ok, detail = _validate_key(api_key)
        _say(f"  {_health_icon(ok)} {detail}")
        if not ok:
            if not _confirm("  Save it anyway?", False):
                return 1
        if not _save_api_key(api_key):
            return 1
        _say("  saved to Hermes' .env (0600)")
    elif not existing:
        return 1

    # 3. Template ------------------------------------------------------
    _say("")
    _say("[3/5] Sandbox template")
    templates = _list_templates()
    if templates:
        _say("  templates already built on this account:")
        for item in templates:
            names = ",".join(str(n) for n in item["names"]) or "-"
            _say(f"    {item['template_id']}  names={names}  "
                 f"cpu={item['cpu']} mem={item['memory']}MB")
    else:
        _say("  no custom templates built yet")

    chosen = "base"
    _say("")
    _say("  The system 'base' template already ships python3.11, node20, git and bash.")
    _say("  A custom template is only needed to raise cpu/memory above 2 vCPU / 512MB")
    _say("  (Novita cannot change those at create time).")
    if getattr(args, "build_template", False) or (
        _tty() and _confirm("  Build a 2 vCPU / 4096MB parity template now?", False)
    ):
        build_args = type(
            "A", (), {"image": DEFAULT_IMAGE, "name": DEFAULT_TEMPLATE_NAME, "cpu": 2, "memory": 4096}
        )()
        if cmd_install_template(build_args) == 0:
            chosen = _config.get_setting("novita_template", "base")
    else:
        _say("  keeping 'base'")
        _set_config("terminal.novita_template", "base")

    # 4. Backend -------------------------------------------------------
    _say("")
    _say("[4/5] Selecting the backend")
    _set_config("terminal.backend", "novita")
    _set_config("terminal.novita_timeout", _config.get_int_setting("novita_timeout", 3600))
    _set_config("terminal.container_persistent", "true")
    _say(f"  terminal.backend = novita")
    _say(f"  terminal.novita_template = {chosen}")
    _say("  terminal.container_persistent = true")

    # 5. Verify --------------------------------------------------------
    _say("")
    _say("[5/5] Verifying end to end")
    try:
        import os

        os.environ["TERMINAL_ENV"] = "novita"
        from .environment import NovitaEnvironment

        env = NovitaEnvironment(cwd="/root", timeout=60, task_id="setup-verify")
        try:
            output, code = env._exec("echo setup-ok && whoami", 30)
            ok = "setup-ok" in (output or "") and code == 0
            _say(f"  {_health_icon(ok)} backend executed through NovitaEnvironment "
                 f"(exit {code}): {(output or '').strip()[:120]}")
            if not ok:
                return 1
        finally:
            try:
                env.cleanup()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        _say(f"  {_health_icon(False)} verification failed: {type(e).__name__}: {e}")
        _say("  Run `hermes novita-sandbox doctor --live` for detail.")
        return 1

    _say("")
    _say("Done. Hermes will now run terminal commands inside Novita sandboxes.")
    _say("  hermes novita-sandbox doctor    # probe report")
    _say("  hermes novita-sandbox status    # sandboxes on the account")
    _say("To revert:  hermes config set terminal.backend local")
    return 0


def cmd_patch_setup(args: Any) -> int:
    """Add the Novita row to `hermes setup`'s terminal backend menu.

    Edits hermes_cli/setup.py, which `hermes update` reverts -- see the
    setup_patch module docstring for why there is no alternative.
    """
    from . import setup_patch

    _say("")
    _say("Novita sandbox backend -- setup menu integration")
    _say("=" * 60)

    info = setup_patch.detect()
    _say(f"  target: {info.get('path', '(not found)')}")
    _say(f"  state : {info['state']}")
    if info.get("detail"):
        _say(f"          {info['detail']}")
    _say("")

    if info["state"] == "shape_unknown":
        _say("REFUSED. Hermes' setup wizard no longer matches the shape this")
        _say("plugin knows how to patch, so nothing was changed. Update the")
        _say("plugin, or use `hermes novita-sandbox setup` instead.")
        return 1

    if info["state"] == "no_setup_module" or info["state"] == "unreadable":
        _say("Cannot patch. Use `hermes novita-sandbox setup` instead.")
        return 1

    ok, message = setup_patch.apply()
    _say(("OK: " if ok else "FAILED: ") + message)
    if not ok:
        return 1

    _say("")
    _say("Novita now appears in `hermes setup` -> Terminal Backend, and selecting")
    _say("it prompts for the API key, installs the SDK, and enables the plugin.")
    _say("")
    _say("NOTE: `hermes update` runs git reset --hard, which reverts this edit.")
    _say("      Re-run `hermes novita-sandbox patch-setup` after any update.")
    _say("      `hermes novita-sandbox doctor` reports it as MISSING if that")
    _say("      happens. Undo with `hermes novita-sandbox unpatch-setup`.")
    return 0


def cmd_unpatch_setup(args: Any) -> int:
    from . import setup_patch

    ok, message = setup_patch.revert()
    _say(("OK: " if ok else "FAILED: ") + message)
    return 0 if ok else 1


_COMMANDS = {
    "setup": cmd_setup,
    "doctor": cmd_doctor,
    "status": cmd_status,
    "install-template": cmd_install_template,
    "patch-setup": cmd_patch_setup,
    "unpatch-setup": cmd_unpatch_setup,
}


def _setup_parser(parser: Any) -> None:
    subparsers = parser.add_subparsers(dest="novita_command")

    setup = subparsers.add_parser("setup", help="Configure the Novita backend interactively")
    setup.add_argument("--api-key", default=None, help="NOVITA_API_KEY (skips the prompt)")
    setup.add_argument("--yes", action="store_true", help="Accept defaults without prompting")
    setup.add_argument(
        "--build-template", action="store_true",
        help="Build the 2 vCPU / 4096MB parity template",
    )

    doctor = subparsers.add_parser("doctor", help="Report patch probes and configuration")
    doctor.add_argument("--live", action="store_true", help="Also create a real sandbox")

    subparsers.add_parser("status", help="Show configuration and sandboxes on the account")

    install = subparsers.add_parser("install-template", help="Build a custom sandbox template")
    install.add_argument("--name", default=DEFAULT_TEMPLATE_NAME)
    install.add_argument("--image", default=DEFAULT_IMAGE)
    install.add_argument("--cpu", type=int, default=2)
    install.add_argument("--memory", type=int, default=4096)

    subparsers.add_parser(
        "patch-setup",
        help="Add Novita to `hermes setup`'s terminal backend menu",
    )
    subparsers.add_parser(
        "unpatch-setup",
        help="Remove the Novita row from `hermes setup`'s menu",
    )


def _dispatch(args: Any) -> int:
    command = getattr(args, "novita_command", None) or "doctor"
    handler = _COMMANDS.get(command)
    if handler is None:
        _say(f"unknown command: {command}")
        _say(f"available: {', '.join(sorted(_COMMANDS))}")
        return 1
    try:
        return handler(args)
    except KeyboardInterrupt:
        _say("\ninterrupted")
        return 130
    except Exception as e:  # noqa: BLE001
        _say(f"error: {type(e).__name__}: {e}")
        return 1


def register_cli(ctx: Any) -> None:
    ctx.register_cli_command(
        name="novita-sandbox",
        help="Novita sandbox terminal backend (setup, doctor, status)",
        setup_fn=_setup_parser,
        handler_fn=_dispatch,
        description=(
            "Configure and inspect the Novita sandbox terminal backend. "
            "Runs in ~/.hermes/plugins/novita-sandbox and modifies no Hermes source."
        ),
    )
