"""Launch several REAL instances of this Django app on different ports.

This is the heart of Requirement #5 the way the brief means it: an *instance*
(a node) is a separate OS PROCESS listening on its own PORT — NOT a thread pool
inside one process. This script starts N copies of `manage.py runserver`, each
on its own port, each tagged with an INSTANCE_PORT/INSTANCE_NAME so every
response can say which instance served it.

    python scripts/start_instances.py                 # ports 8001 8002 8003
    python scripts/start_instances.py --ports 8001 8002 8003 8004

Leave it running, then in another terminal drive load through the balancer:

    python manage.py demo_load_distribution_real

Stop everything with Ctrl+C in this window.

Note: with the default SQLite database, instances share one file; that is fine
for the read-heavy whoami/cached-read load test. For heavy concurrent WRITES use
Postgres (already supported via the DB_* env vars in settings).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def start_instance(port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["INSTANCE_PORT"] = str(port)
    env["INSTANCE_NAME"] = f"instance-{port}"
    # --noreload: one process per instance (the autoreloader would fork a second
    # child and muddy the "one process = one instance" model).
    cmd = [
        sys.executable, str(BASE_DIR / "manage.py"),
        "runserver", f"127.0.0.1:{port}", "--noreload",
    ]
    print(f"  starting {env['INSTANCE_NAME']}  ->  http://127.0.0.1:{port}/")
    return subprocess.Popen(cmd, env=env, cwd=str(BASE_DIR))


def main():
    parser = argparse.ArgumentParser(description="Start N Django instances on different ports.")
    parser.add_argument("--ports", type=int, nargs="+", default=[8001, 8002, 8003])
    args = parser.parse_args()

    print("=" * 64)
    print(" Req 5 — launching REAL instances (separate processes / ports)")
    print("=" * 64)

    procs: list[subprocess.Popen] = []
    for port in args.ports:
        procs.append(start_instance(port))
        time.sleep(0.4)  # small stagger so startup logs don't interleave

    print("-" * 64)
    print(f" {len(procs)} instances running on ports: "
          f"{', '.join(str(p) for p in args.ports)}")
    print(" Drive load through the balancer in another terminal:")
    print("   python manage.py demo_load_distribution_real")
    print(" Press Ctrl+C here to stop all instances.")
    print("-" * 64)

    try:
        # Wait until interrupted; if any instance dies, report it.
        while True:
            time.sleep(1)
            for port, proc in zip(args.ports, procs):
                if proc.poll() is not None:
                    print(f" !! instance on port {port} exited "
                          f"(code {proc.returncode})")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\n stopping all instances...")
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()   # SIGTERM / TerminateProcess — stop the instance
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(" all instances stopped.")


if __name__ == "__main__":
    main()
