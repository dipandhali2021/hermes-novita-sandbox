"""Live probe #3: auth scoping, resume, resize, lifecycle construction.

Probe #2 revealed that bare ``Sandbox.list()`` classmethods raise
AuthenticationException because the key was only passed to the Novita client,
never exported. This probe determines the correct auth strategy for the plugin
and settles resume/resize/lifecycle.

Run:  ~/.hermes/hermes-agent/venv/bin/python probes/probe_auth_resume.py
"""

import os
import sys
from pathlib import Path

HERMES_ENV = Path.home() / ".hermes" / ".env"


def load_key() -> str:
    for line in HERMES_ENV.read_text().splitlines():
        if line.startswith("NOVITA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("NOVITA_API_KEY not found")


KEY = load_key()
# The plugin will do exactly this before any SDK call.
os.environ["NOVITA_API_KEY"] = KEY

from novita_sandbox import Novita, Sandbox, SandboxQuery, SandboxState  # noqa: E402


def check(label, fn):
    try:
        v = fn()
        print(f"  [ok]   {label}: {repr(v)[:200]}")
        return v
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {label}: {type(e).__name__}: {str(e)[:180]}")
        return None


def main() -> int:
    novita = Novita(api_key=KEY)
    TAG = {"hermes_probe3": "1"}

    print("=" * 70)
    print("[A] lifecycle construction + create with pause/auto_resume")
    print("=" * 70)
    try:
        from novita_sandbox import SandboxOnTimeout  # type: ignore
        print(f"  SandboxOnTimeout members: {[m for m in dir(SandboxOnTimeout) if not m.startswith('_')]}")
    except Exception as e:  # noqa: BLE001
        print(f"  SandboxOnTimeout not at top level: {type(e).__name__}")
        try:
            from novita_sandbox.core.sandbox.sandbox_api import SandboxOnTimeout as SOT  # type: ignore
            print(f"  core path: {[m for m in dir(SOT) if not m.startswith('_')]}")
        except Exception as e2:  # noqa: BLE001
            print(f"  core path FAIL: {e2}")

    sb = None
    created = check("create(lifecycle=dict pause/auto_resume)",
                    lambda: novita.sandbox.create(
                        "base", timeout=300, metadata=TAG,
                        lifecycle={"on_timeout": "pause", "auto_resume": True}))
    if created is None:
        print("  retrying with lifecycle omitted, then pause() manually")
        created = check("create(plain)", lambda: novita.sandbox.create("base", timeout=300, metadata=TAG))
    sb = created
    if sb is None:
        print("FATAL: could not create sandbox")
        return 1

    sb_id = sb.sandbox_id
    print(f"  sandbox_id={sb_id}")
    info = check("get_info()", lambda: sb.get_info())
    if info is not None:
        print(f"    lifecycle={info.lifecycle} cpu={info.cpu_count} mem={info.memory_mb}MB")

    print("\n" + "=" * 70)
    print("[B] set_timeout unit (create used 300 -> is it seconds?)")
    print("=" * 70)
    before = check("end_at before", lambda: sb.get_info().end_at)
    check("set_timeout(600)", lambda: sb.set_timeout(600))
    after = check("end_at after", lambda: sb.get_info().end_at)
    if before and after:
        print(f"    delta = {(after - before).total_seconds():.0f}s  (600 => seconds confirmed)")

    print("\n" + "=" * 70)
    print("[C] resize(memory_mib) - can we get 4GB without a custom template?")
    print("=" * 70)
    check("resize(memory_mib=4096)", lambda: sb.resize(memory_mib=4096))
    check("mem after resize", lambda: sb.get_info().memory_mb)

    print("\n" + "=" * 70)
    print("[D] Auth scoping: which list/connect calls work?")
    print("=" * 70)
    check("write marker", lambda: sb.commands.run("echo persisted > /root/.hermes_marker").stdout)
    check("pause", lambda: sb.pause())
    print(f"  paused {sb_id}")

    found = None
    # (1) client namespace, metadata filter
    def via_client():
        pag = novita.sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED], metadata=TAG))
        out = []
        while True:
            items = pag.next_items()
            if not items:
                break
            out.extend(items)
        return out

    res = check("novita.sandbox.list(...)", lambda: [(i.sandbox_id, str(i.state)) for i in via_client()])
    if res:
        found = res[0][0]

    # (2) bare classmethod with explicit key
    res2 = check("Sandbox.list(api_key=...)",
                 lambda: [(i.sandbox_id, str(i.state)) for i in
                          (lambda p: (lambda: [x for pg in iter(lambda: p.next_items(), []) for x in pg])())(
                              Sandbox.list(query=SandboxQuery(state=[SandboxState.PAUSED], metadata=TAG),
                                           api_key=KEY))])
    if found is None and res2:
        found = res2[0][0]

    print("\n" + "=" * 70)
    print("[E] Resume a paused sandbox")
    print("=" * 70)
    if found:
        re1 = check("novita.sandbox.connect(paused)", lambda: novita.sandbox.connect(found, timeout=300))
        if re1:
            check("marker survived pause/resume",
                  lambda: re1.commands.run("cat /root/.hermes_marker").stdout.strip())
            check("state after connect", lambda: re1.get_info().state)
            check("kill", lambda: re1.kill())
            sb = None
    else:
        print("  no paused sandbox found to resume")

    print("\n" + "=" * 70)
    print("[F] Cleanup sweep")
    print("=" * 70)
    try:
        pag = novita.sandbox.list(query=SandboxQuery(state=[SandboxState.RUNNING, SandboxState.PAUSED]))
        n = 0
        while True:
            items = pag.next_items()
            if not items:
                break
            for it in items:
                n += 1
                if any(k.startswith("hermes_probe") for k in it.metadata):
                    print(f"  leftover {it.sandbox_id} ({it.state}) -> kill")
                    try:
                        novita.sandbox.connect(it.sandbox_id).kill()
                    except Exception as e:  # noqa: BLE001
                        print(f"    kill failed: {e}")
        print(f"  scanned {n}")
    except Exception as e:  # noqa: BLE001
        print(f"  sweep FAIL: {type(e).__name__}: {e}")

    if sb is not None:
        try:
            sb.kill()
            print(f"  killed {sb_id}")
        except Exception as e:  # noqa: BLE001
            print(f"  kill failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
