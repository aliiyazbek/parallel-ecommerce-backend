"""One-command proof that the cache is DISTRIBUTED across real processes (Req 6).

It is easy to claim "the cache is shared." This script proves it end to end:

    1. Kill any stale instances and start 3 fresh ones on ports 8001/8002/8003
       (separate OS processes — see scripts/start_instances.py).
    2. Clear Redis so the cache starts COLD.
    3. Read the same product through each instance in turn:
         - instance 8001  -> cache MISS  (queries the DB, fills Redis)
         - instance 8002  -> cache HIT   (served from SHARED Redis, no DB query)
         - instance 8003  -> cache HIT
    4. Show the single shared key in Redis that all three processes read.
    5. Stop the instances.

A per-process dictionary could never produce step 3: instance 8002 never queried
the database for this product, yet it serves it — because the value lives in the
shared Redis that every instance sees. That is what makes the cache *distributed*.

Run (Redis/Memurai must be on localhost:6379):

    python scripts/demo_shared_cache.py
    python scripts/demo_shared_cache.py --ports 8001 8002 8003 --product-stock 1000
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
BANNER = "=" * 72
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def run_manage(*args) -> subprocess.CompletedProcess:
    return subprocess.run([PY, os.path.join(BASE_DIR, "manage.py"), *args],
                          cwd=BASE_DIR, capture_output=True, text=True)


def kill_stale_instances():
    """Kill any lingering `runserver` processes so ports are free and no stale
    code answers our requests (Windows detaches them from the launcher)."""
    if os.name == "nt":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'runserver' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, text=True,
        )
    else:
        subprocess.run(["pkill", "-f", "manage.py runserver"], capture_output=True)
    time.sleep(1.0)


def get_redis():
    try:
        import redis
        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1,
                                 decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def start_instances(ports: list[int]) -> list[subprocess.Popen]:
    procs = []
    for port in ports:
        env = dict(os.environ)
        env["INSTANCE_PORT"] = str(port)
        env["INSTANCE_NAME"] = f"instance-{port}"
        procs.append(subprocess.Popen(
            [PY, os.path.join(BASE_DIR, "manage.py"),
             "runserver", f"127.0.0.1:{port}", "--noreload"],
            env=env, cwd=BASE_DIR,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
    return procs


def wait_until_up(ports: list[int], timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ok = all(
                requests.get(f"http://127.0.0.1:{p}/api/whoami/", timeout=1).status_code == 200
                for p in ports
            )
            if ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.6)
    return False


def read_product(port: int, product_id: int) -> dict:
    r = requests.get(f"http://127.0.0.1:{port}/api/whoami/",
                     params={"product": product_id}, timeout=5)
    return r.json()


def main():
    ap = argparse.ArgumentParser(description="Prove the cache is shared across processes (Req 6).")
    ap.add_argument("--ports", type=int, nargs="+", default=[8001, 8002, 8003])
    ap.add_argument("--product-stock", type=int, default=1000)
    args = ap.parse_args()
    ports = args.ports

    print(BANNER)
    print(" Req 6 - DISTRIBUTED CACHE shared across real processes")
    print(BANNER)

    redis_client = get_redis()
    if redis_client is None:
        print(" !! Redis not reachable at", REDIS_URL)
        print("    Start it (Memurai service on Windows, or `docker run -p 6379:6379 redis`)")
        print("    NOTE: without real Redis each process has its OWN fallback cache,")
        print("          so this cross-process proof cannot hold.")
        return 1

    print(" Seeding a product and clearing Redis (cold cache) ...")
    seed = run_manage("load_test_seed", "--stock", str(args.product_stock))
    # Parse the product id out of "Created/Reset product #<id> …".
    product_id = None
    for tok in seed.stdout.replace("#", " ").split():
        if tok.isdigit():
            product_id = int(tok)
            break
    if product_id is None:
        print(" !! could not seed product:", seed.stdout, seed.stderr)
        return 1
    redis_client.flushdb()
    print(f"   product id = {product_id}, Redis flushed.")

    print(f" Killing stale instances and starting {len(ports)} fresh ones on ports "
          f"{', '.join(map(str, ports))} ...")
    kill_stale_instances()
    procs = start_instances(ports)
    try:
        if not wait_until_up(ports):
            print(" !! instances did not come up in time.")
            return 1

        print()
        print(" Reading the SAME product through each instance:")
        print(" " + "-" * 70)
        results = []
        for i, port in enumerate(ports):
            res = read_product(port, product_id)
            hit = res.get("served_from_cache")
            kind = "HIT  (served from SHARED Redis - no DB query)" if hit else \
                   "MISS (queried DB, then filled Redis)"
            tag = "" if i else "   <- first read, cold cache"
            print(f"   instance-{res.get('port')} (pid {res.get('pid')})  "
                  f"served_from_cache={str(hit):<5}  {kind}{tag}")
            results.append(hit)

        print(" " + "-" * 70)
        keys = redis_client.keys("product:*")
        print(f" Shared Redis now holds ONE key for this product: {keys}")
        print()

        first_miss = results and results[0] is False
        rest_hits = all(results[1:]) if len(results) > 1 else False
        if first_miss and rest_hits:
            print(" RESULT: [PROVEN] the first process MISSED and loaded the DB;")
            print("         every OTHER process served the product from the shared")
            print("         Redis cache without touching the DB. The cache is")
            print("         genuinely DISTRIBUTED across separate processes.")
        else:
            print(" RESULT: [UNEXPECTED] hit/miss pattern:", results)
            print("         (If the first read was already a HIT, Redis was not cold -")
            print("          re-run; the script flushes Redis at the start.)")
        print(BANNER)
        return 0
    finally:
        print(" Stopping instances ...")
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        kill_stale_instances()
        print(" Done.")


if __name__ == "__main__":
    sys.exit(main())
