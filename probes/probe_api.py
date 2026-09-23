"""Live API probe for the Novita Sandbox SDK.

Throwaway verification harness. Settles the open items in section 12 of
docs/2026-09-23-novita-sandbox-backend-design.md against the real service
instead of trusting doc-derived inferences.

Run:  ~/.hermes/hermes-agent/venv/bin/python probes/probe_api.py

Never prints the API key.
"""

import os
import sys
import traceback
from pathlib import Path

HERMES_ENV = Path.home() / ".hermes" / ".env"


def load_key() -> str:
    key = os.environ.get("NOVITA_API_KEY")
    if key:
        return key
    if HERMES_ENV.is_file():
        for line in HERMES_ENV.read_text().splitlines():
            if line.startswith("NOVITA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("NOVITA_API_KEY not found")


def check(label, fn):
    """Run one probe, never let it abort the rest."""
    try:
        value = fn()
        print(f"  [ok]   {label}: {value!r}")
        return value
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")
        return None


def main() -> int:
    key = load_key()
    print("=" * 72)
    print("NOVITA SANDBOX API PROBE")
    print("=" * 72)

    # ---------------------------------------------------------------- imports
    print("\n[1] Import surface")
    import novita_sandbox

    print(f"  version: {getattr(novita_sandbox, '__version__', 'unknown')}")
    print(f"  top-level exports: {sorted(n for n in dir(novita_sandbox) if not n.startswith('_'))[:25]}")

    Novita = getattr(novita_sandbox, "Novita", None)
    SandboxQuery = getattr(novita_sandbox, "SandboxQuery", None)
    SandboxState = getattr(novita_sandbox, "SandboxState", None)
    print(f"  Novita={Novita is not None} SandboxQuery={SandboxQuery is not None} SandboxState={SandboxState is not None}")

    if SandboxState is not None:
        print(f"  SandboxState members: {[m for m in dir(SandboxState) if not m.startswith('_')]}")

    # locate the exit exception class without assuming its module
    exit_exc = None
    for name in ("CommandExitException", "CommandExitError"):
        exit_exc = getattr(novita_sandbox, name, None)
        if exit_exc:
            print(f"  found {name} at novita_sandbox.{name}")
            break
    if exit_exc is None:
        try:
            from novita_sandbox.core.commands import CommandExitException as exit_exc  # type: ignore
            print("  found CommandExitException at novita_sandbox.core.commands")
        except Exception as e:  # noqa: BLE001
            print(f"  exit exception not at top level/core.commands ({type(e).__name__})")

    if Novita is None:
        print("\nFATAL: Novita client missing; aborting")
        return 1

    novita = Novita(api_key=key)
    print(f"  client namespaces: {[n for n in dir(novita) if not n.startswith('_')]}")

    try:
        print(f"  novita.sandbox methods: {[m for m in dir(novita.sandbox) if not m.startswith('_')]}")
    except Exception as e:  # noqa: BLE001
        print(f"  cannot introspect novita.sandbox: {e}")

    # ------------------------------------------------------------ create/exec
    print("\n[2] Create sandbox (base template, timeout=180s, self-cleaning)")
    sandbox = None
    try:
        try:
            sandbox = novita.sandbox.create(
                "base", timeout=180, metadata={"hermes_probe": "1"}
            )
        except Exception as e:  # noqa: BLE001
            print(f"  create('base') failed: {type(e).__name__}: {e}")
            print("  retrying create() with no template arg")
            sandbox = novita.sandbox.create(timeout=180, metadata={"hermes_probe": "1"})

        print(f"  [ok]   sandbox_id={sandbox.sandbox_id}")
        print(f"  sandbox attrs: {[a for a in dir(sandbox) if not a.startswith('_')]}")
        check("get_info()", lambda: sandbox.get_info())
        check("commands attrs", lambda: [m for m in dir(sandbox.commands) if not m.startswith('_')])
        check("files attrs", lambda: [m for m in dir(sandbox.files) if not m.startswith('_')])

        print("\n[3] Environment introspection")
        for label, cmd in [
            ("HOME", "echo $HOME"),
            ("whoami", "whoami"),
            ("pwd", "pwd"),
            ("uname", "uname -srm"),
            ("bash", "bash --version | head -1"),
            ("python3", "python3 --version 2>&1"),
            ("node", "node --version 2>&1 || echo MISSING"),
            ("git", "git --version 2>&1 || echo MISSING"),
            ("tar", "tar --version 2>&1 | head -1 || echo MISSING"),
            ("shasum", "which sha1sum sha256sum 2>&1 || echo MISSING"),
        ]:
            r = check(label, lambda c=cmd: sandbox.commands.run(c).stdout.strip())
            del r

        print("\n[4] CommandResult shape + non-zero exit behaviour (CRITICAL)")
        r = sandbox.commands.run("echo out; echo err 1>&2; exit 0")
        print(f"  zero-exit result type: {type(r).__name__}")
        print(f"    .stdout={r.stdout!r}")
        print(f"    .stderr={getattr(r, 'stderr', '<absent>')!r}")
        print(f"    .exit_code={getattr(r, 'exit_code', '<absent>')!r}")
        print(f"    .error={getattr(r, 'error', '<absent>')!r}")

        print("  running: echo before; exit 42  (expect raise, not return)")
        try:
            r2 = sandbox.commands.run("echo before; exit 42")
            print(f"    NO RAISE — returned {type(r2).__name__} exit_code={getattr(r2, 'exit_code', '<absent>')!r} stdout={r2.stdout!r}")
            print("    => must read exit_code from the return value, no unwrap needed")
        except Exception as e:  # noqa: BLE001
            print(f"    RAISED {type(e).__name__}: {e}")
            print(f"    mro: {[c.__name__ for c in type(e).__mro__]}")
            for attr in ("stdout", "stderr", "exit_code", "error", "message"):
                if hasattr(e, attr):
                    print(f"    .{attr}={getattr(e, attr)!r}")
            print(f"    all attrs: {[a for a in dir(e) if not a.startswith('_')]}")
            if exit_exc is not None:
                print(f"    isinstance(CommandExitException)={isinstance(e, exit_exc)}")

        print("\n[5] Timeout option")
        check("run(timeout=5)", lambda: sandbox.commands.run("echo timed", timeout=5).stdout.strip())

        print("\n[6] Filesystem API")
        check("files.write(path, bytes)", lambda: sandbox.files.write("/tmp/probe_a.txt", b"hello-bytes"))
        check("files.read -> type", lambda: (type(sandbox.files.read("/tmp/probe_a.txt")).__name__,
                                             repr(sandbox.files.read("/tmp/probe_a.txt"))[:80]))
        check("files.write(path, str)", lambda: sandbox.files.write("/tmp/probe_b.txt", "hello-str"))
        check("files.write_files batch", lambda: sandbox.files.write_files([
            {"path": "/tmp/probe_c1.txt", "data": b"c1"},
            {"path": "/tmp/probe_c2.txt", "data": b"c2"},
        ]))
        check("files.list", lambda: sandbox.files.list("/tmp"))
        # binary round-trip: this is what bulk_download needs
        check("make binary tar", lambda: sandbox.commands.run(
            "printf 'A\\x00\\xffB' > /tmp/probe_bin.dat; tar cf /tmp/probe.tar -C / tmp/probe_a.txt; ls -l /tmp/probe.tar").stdout.strip())
        raw = check("files.read(tar) raw type", lambda: sandbox.files.read("/tmp/probe.tar"))
        if raw is not None:
            data = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
            print(f"    tar read returned {type(raw).__name__}, {len(data)} bytes, head={bytes(data[:8])!r}")

        print("\n[7] mkdir behaviour for nested upload (sync path needs this)")
        check("mkdir -p nested", lambda: sandbox.commands.run(
            "mkdir -p /home/user/.hermes/skills/x && echo made").stdout.strip())
        check("write into nested", lambda: sandbox.files.write("/home/user/.hermes/skills/x/f.txt", b"nested"))

    except Exception as e:  # noqa: BLE001
        print(f"\nUNEXPECTED FAILURE: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if sandbox is not None:
            print("\n[8] Cleanup")
            check("kill()", lambda: sandbox.kill())
            print("  sandbox killed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
