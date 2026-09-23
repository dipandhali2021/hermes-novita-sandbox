"""Load the plugin through Hermes' real plugin manager and show the report.

Verifies the two things that matter most:
  1. register() runs under the real loader without raising.
  2. The injection probes report accurately, and `terminal.backend: novita`
     actually resolves to NovitaEnvironment through the core factory.

Run:  ~/.hermes/hermes-agent/venv/bin/python probes/probe_plugin_load.py
"""

import os
import sys
from pathlib import Path

REPO = Path.home() / ".hermes" / "hermes-agent"
sys.path.insert(0, str(REPO))
os.chdir(REPO)


def main() -> int:
    print("=" * 70)
    print("PLUGIN LOAD INTEGRATION TEST")
    print("=" * 70)

    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)

    loaded = None
    for name, entry in getattr(manager, "_plugins", {}).items():
        if "novita" in str(name):
            loaded = entry
            print(f"  discovered plugin: {name}")
            print(f"    source={getattr(entry, 'source', '?')} "
                  f"enabled={getattr(entry, 'enabled', '?')} "
                  f"error={getattr(entry, 'error', None)}")
    if loaded is None:
        print("  FAIL: plugin not discovered")
        print(f"  known: {list(getattr(manager, '_plugins', {}).keys())}")
        return 1
    if not getattr(loaded, "enabled", False):
        print(f"  FAIL: plugin discovered but not enabled: {getattr(loaded, 'error', '')}")
        return 1

    module = getattr(loaded, "module", None)
    if module is None:
        print("  FAIL: plugin module not loaded")
        return 1

    report = getattr(module, "get_report", lambda: None)()
    if report is None:
        print("  FAIL: register() did not produce a report")
        return 1

    print()
    print(report.format_text())
    print()
    print(f"  available={report.available}")
    for d in report.degradations():
        print(f"  degradation: {d.name} -- {d.detail}")

    # CLI command registered?
    cli_cmds = getattr(manager, "_cli_commands", {})
    print(f"\n  CLI command registered: {'novita-sandbox' in cli_cmds}")

    # The real end-to-end assertion: does the core factory route to us?
    print("\n" + "=" * 70)
    print("FACTORY ROUTING TEST")
    print("=" * 70)
    import tools.terminal_tool as tt

    factory = tt._create_environment
    print(f"  wrapped={getattr(factory, '__novita_wrapped__', False)}")

    if not report.available:
        print("  backend unavailable; skipping construction test")
        return 0

    try:
        env = factory(
            env_type="novita",
            image="",
            cwd="/root",
            timeout=60,
            task_id="probe-load",
        )
        print(f"  factory returned: {type(env).__name__}")
        assert type(env).__name__ == "NovitaEnvironment", "wrong class!"

        out, code = env._exec("echo routed-ok && pwd && python3 --version", 60)
        print(f"  exec exit={code} out={out.strip()!r}")
        assert "routed-ok" in out, "command did not run in the sandbox"

        # Non-zero exit must return a code, not raise (the critical trap).
        out2, code2 = env._exec("echo before; exit 42", 60)
        print(f"  non-zero exit test: exit={code2} out={out2.strip()!r}")
        assert code2 == 42, f"expected exit code 42, got {code2}"

        print("\n  OK: factory routes to NovitaEnvironment and exits are unwrapped")
        try:
            env.cleanup()
            print("  cleanup() completed")
        except Exception as e:  # noqa: BLE001
            print(f"  cleanup raised: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        import traceback

        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
