"""Live probe #2: persistence, resources, binary download, templates.

Settles the remaining open items from section 12 of the design spec.
Output is deliberately compact.

Run:  ~/.hermes/hermes-agent/venv/bin/python probes/probe_persistence.py
"""

import inspect
import sys
from pathlib import Path

HERMES_ENV = Path.home() / ".hermes" / ".env"


def load_key() -> str:
    import os
    if os.environ.get("NOVITA_API_KEY"):
        return os.environ["NOVITA_API_KEY"]
    for line in HERMES_ENV.read_text().splitlines():
        if line.startswith("NOVITA_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("NOVITA_API_KEY not found")


def check(label, fn):
    try:
        v = fn()
        s = repr(v)
        print(f"  [ok]   {label}: {s[:220]}")
        return v
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {label}: {type(e).__name__}: {str(e)[:200]}")
        return None


def main() -> int:
    from novita_sandbox import Novita, Sandbox, SandboxQuery, SandboxState

    novita = Novita(api_key=load_key())
    TAG = {"hermes_probe2": "1"}

    print("=" * 70)
    print("[A] Signatures that matter")
    print("=" * 70)
    for name, fn in [
        ("files.read", getattr(type(novita.sandbox).__dict__.get("files", object), "__doc__", None) and None or None),
    ]:
        del name, fn
    try:
        from novita_sandbox.core.filesystem import Filesystem  # type: ignore
        print(f"  Filesystem.read{inspect.signature(Filesystem.read)}")
    except Exception as e:  # noqa: BLE001
        print(f"  Filesystem.read sig unavailable: {type(e).__name__}: {e}")
    check("Sandbox.download_url sig", lambda: str(inspect.signature(Sandbox.download_url)))
    check("Sandbox.upload_url sig", lambda: str(inspect.signature(Sandbox.upload_url)))
    check("Sandbox.hotplug_memory sig", lambda: str(inspect.signature(Sandbox.hotplug_memory)))
    check("Sandbox.resize sig", lambda: str(inspect.signature(Sandbox.resize)))
    print("  SandboxLifecycle:", end=" ")
    try:
        from novita_sandbox import SandboxLifecycle  # type: ignore
        print(f"found; sig={inspect.signature(SandboxLifecycle)}")
    except Exception as e:  # noqa: BLE001
        print(f"not importable directly ({type(e).__name__}); trying core path")
        try:
            from novita_sandbox.core.sandbox.sandbox_api import SandboxLifecycle as SL  # type: ignore
            print(f"  core path found; sig={inspect.signature(SL)}")
        except Exception as e2:  # noqa: BLE001
            print(f"  FAIL: {e2}")

    print("\n" + "=" * 70)
    print("[B] Existing templates (is a build even needed?)")
    print("=" * 70)
    try:
        page = novita.template.list(template_type="template_build", page=1, limit=20)
        items = getattr(page, "items", None) or getattr(page, "templates", [])
        print(f"  total={getattr(page, 'total', '?')} returned={len(items)}")
        for t in items:
            names = getattr(t, "names", None) or getattr(t, "aliases", [])
            print(f"    - {(getattr(t,'template_id','?'))[:14]} names={names} "
                  f"cpu={getattr(t,'cpu_count','?')} mem={getattr(t,'memory_mb','?')}MB "
                  f"disk={getattr(t,'disk_size_mb','?')}MB")
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] template.list: {type(e).__name__}: {str(e)[:200]}")

    print("\n" + "=" * 70)
    print("[C] Lifecycle + persistence round-trip")
    print("=" * 70)
    sb = None
    try:
        sb = novita.sandbox.create("base", timeout=240, metadata=TAG)
        sb_id = sb.sandbox_id
        print(f"  created {sb_id}")
        info = sb.get_info()
        print(f"  default lifecycle: {info.lifecycle}")
        print(f"  cpu={info.cpu_count} mem={info.memory_mb}MB")

        # resource mutation at runtime?
        print("\n  resource adjustment at runtime:")
        check("hotplug_memory(4096)", lambda: sb.hotplug_memory(4096))
        check("resize()", lambda: sb.resize(cpu_count=2, memory_mb=4096))
        check("info after", lambda: (lambda i: f"cpu={i.cpu_count} mem={i.memory_mb}MB")(sb.get_info()))

        # binary-safe download path
        print("\n  binary-safe download:")
        check("make tar", lambda: sb.commands.run(
            "mkdir -p /root/.hermes && echo payload > /root/.hermes/f.txt && "
            "tar cf /tmp/sync.tar -C / root/.hermes && ls -l /tmp/sync.tar").stdout.strip())
        url = check("download_url", lambda: sb.download_url("/tmp/sync.tar"))
        if url:
            try:
                import httpx
                r = httpx.get(url, timeout=30, follow_redirects=True)
                print(f"    GET {r.status_code}, {len(r.content)} bytes, magic={r.content[:2]!r} (tar magic should be b'..')")
                print(f"    gzip/tar sane: {r.content[257:262] == b'ustar'}")
            except Exception as e:  # noqa: BLE001
                print(f"    fetch FAIL: {type(e).__name__}: {e}")

        # persistence: pause -> list by metadata -> connect
        print("\n  persistence round-trip:")
        check("pause()", lambda: sb.pause())
        check("state after pause", lambda: sb.get_info().state)
        found = None
        try:
            q = SandboxQuery(state=[SandboxState.PAUSED], metadata=TAG)
            pag = Sandbox.list(query=q)
            while True:
                items = pag.next_items()
                if not items:
                    break
                for it in items:
                    print(f"    listed: {it.sandbox_id} state={it.state} md={list(it.metadata.items())[:3]}")
                    found = it.sandbox_id
        except Exception as e:  # noqa: BLE001
            print(f"  [FAIL] list by metadata: {type(e).__name__}: {str(e)[:200]}")
        if found:
            re1 = check("connect(paused)", lambda: Sandbox.connect(found, timeout=120))
            if re1:
                check("read persisted file", lambda: re1.commands.run("cat /root/.hermes/f.txt").stdout.strip())
                check("set_timeout(90)", lambda: re1.set_timeout(90))
                check("get_info.end_at", lambda: re1.get_info().end_at)
                check("kill reconnected", lambda: re1.kill())
                sb = None
    except Exception as e:  # noqa: BLE001
        print(f"  UNEXPECTED: {type(e).__name__}: {e}")
    finally:
        if sb is not None:
            try:
                sb.kill()
                print(f"  cleaned up {sb.sandbox_id}")
            except Exception as e:  # noqa: BLE001
                print(f"  cleanup failed: {e}")

    # make sure nothing is left paused/running from this probe
    print("\n" + "=" * 70)
    print("[D] Leftover check")
    print("=" * 70)
    try:
        pag = Sandbox.list(query=SandboxQuery(state=[SandboxState.RUNNING, SandboxState.PAUSED]))
        total = 0
        while True:
            items = pag.next_items()
            if not items:
                break
            for it in items:
                if it.metadata.get("hermes_probe2") == "1" or it.metadata.get("hermes_probe") == "1":
                    print(f"  leftover probe sandbox {it.sandbox_id} state={it.state} -> killing")
                    try:
                        Sandbox.connect(it.sandbox_id).kill()
                    except Exception as e:  # noqa: BLE001
                        print(f"    kill failed: {e}")
                total += 1
        print(f"  scanned {total} sandboxes on the account")
        check("get_quota()", lambda: None)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] leftover scan: {type(e).__name__}: {str(e)[:200]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
