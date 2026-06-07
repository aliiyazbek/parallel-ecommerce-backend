# Requirements 5, 7 & 8 — Design, AOP, and Before/After Impact

**Project:** High-Performance E-Commerce Backend Engine (Parallel Programming, 2026)
**Scope of this document:** the three requirements added in this iteration —
**#5 Load Distribution**, **#7 Concurrency Control (with a *distributed* lock)**,
and **#8 Transaction Integrity (ACID)** — how each is implemented, how the
Aspect-Oriented Programming (AOP) layer monitors them, and the measured
*before vs after* effect.

Everything here is demonstrable with three management commands that print a
side-by-side comparison and write a Markdown report into `reports/`:

```bash
python manage.py demo_load_distribution        # Req 5
python manage.py demo_distributed_lock         # Req 7
python manage.py demo_transaction_integrity    # Req 8
```

---

## 0. How these three requirements fit together

They are not independent — they form one story about a *scaled-out* system:

1. **Req 5 (Load Distribution)** says: one machine is not enough, so we run
   several application nodes behind a load balancer.
2. The moment there is **more than one node**, the in-process locks the project
   already used (`threading.Lock`, `BoundedSemaphore`) stop working for shared
   state, because a lock in node A is invisible to node B.
3. **Req 7 (Concurrency Control)** therefore needs a **distributed lock** — a
   lock that lives *outside* every node, in Redis, so all nodes coordinate.
4. **Req 8 (Transaction Integrity)** guarantees that the multi-step purchase
   (charge + stock + order) is all-or-nothing *even while* many nodes hit it at
   once.

So Req 5 creates the multi-process world, Req 7 makes shared-resource updates
safe in that world, and Req 8 makes the composite write safe within each node.

---

## 1. The AOP layer (cross-cutting performance monitoring)

**Aspect-Oriented Programming** keeps a *cross-cutting concern* (something every
operation needs, like timing or logging) out of the business logic. Instead of
sprinkling `t0 = time.time()` all over the services, we attach a **decorator**
that wraps the function. The function does not know it is being measured.

The project already had `core/aop.py` with `@log_execution` and `@audit_action`.
We added a **performance aspect**:

| Piece | File | Role |
|-------|------|------|
| `@measure(name)` | `core/aop.py` | Decorator: times a call, records the sample, never touches the function body. |
| `PerfCollector` | `core/aop.py` | Thread-safe sink that aggregates samples into count / mean / p50 / p95 / min / max. |
| `perf` | `core/aop.py` | The process-wide collector instance the demos read from. |

Example — the distributed-lock service is decorated, nothing else changes:

```python
@measure("stock.dlock_safe")
def decrement_stock_dlock_safe(product_id, qty=1):
    with DistributedLock(f"stock:{product_id}", ttl=5):
        ...        # pure business logic, no timing code in here
```

The Req 7 demo then asks the collector `perf.stats("stock.dlock_safe")` and prints
the p50/p95 of the critical section — the latency numbers in the report come
entirely from the AOP aspect, not from code embedded in the service. This is the
"AOP for performance monitoring" the project brief asks for, applied to the new
requirements.

---

## 2. Requirement 7 — Concurrency Control with a **distributed lock**

### The problem
`select_for_update()` (a DB row lock) and `threading.Lock` both coordinate within
**one** process/transaction. Any logic that crosses processes — e.g. reading a
value into application memory (or a cache), computing on it, then writing it back
— is not protected once requests are spread across nodes. Two nodes read the
same stock, both pass the check, both write → **lost update across machines**.

### The solution — `core/distributed_lock.py`
A real single-instance Redis lock:

```
acquire:  SET lock:<key> <token> NX PX <ttl>     # atomic "set if absent" + auto-expiry
release:  <Lua>: delete the key ONLY if its value still equals <token>
```

- **`NX`** ("set if Not eXists") is atomic in Redis → exactly one node wins.
- **`PX <ttl>`** auto-expires the key → a crashed holder cannot freeze the
  resource forever (deadlock freedom).
- **Unique `token` + Lua compare-and-delete** → a node whose lease already
  expired cannot delete a lock that a *different* node has since taken
  (safe release).

If Redis is not reachable, the lock **falls back to an in-process lock** and
clearly labels itself `memory (in-process fallback — NOT cross-process)` in both
`DistributedLock.backend` and every report, so the limitation is never hidden.
To run the *real* distributed lock:

```bash
docker run --rm -p 6379:6379 redis      # or Memurai on Windows
```

### Where it is applied
- Lock primitive: `core/distributed_lock.py::DistributedLock`
- Safe service: `orders/services.py::decrement_stock_dlock_safe`
- Unsafe baseline (to reproduce the bug): `..._dlock_unsafe`

### Before vs After (measured)
50 workers each buy 1 unit of a product with **stock = 10**:

| Metric | BEFORE (no lock) | AFTER (distributed lock) |
|--------|-----------------:|-------------------------:|
| Successful purchases | 50 | 10 |
| Final stock in DB | 9 | 0 |
| **Oversold by** | **40 units** | **0 units** |
| Verdict | BUG REPRODUCED | stock limit respected |

**Impact:** the lock turns a 40-unit oversell (selling stock that does not exist)
into zero. Only one worker — on any node — is inside the critical section at a
time, so the stock limit is enforced exactly. The AOP `@measure` aspect shows the
trade-off: the safe path has a slightly higher and *tighter* p95 (work is
serialized) versus the unsafe path's wider spread.

---

## 3. Requirement 8 — Transaction Integrity (ACID)

### The problem
A purchase is a **composite** operation:
1. charge the wallet,
2. decrement stock,
3. create the order.

If step 3 fails after steps 1–2 already committed, the customer was **charged
with no order** and stock vanished — a corrupted, inconsistent database. This is
the **A** (atomicity) and **C** (consistency) of ACID.

### The solution — `orders/services.py`
- `purchase_non_atomic` — each step commits on its own (broken baseline).
- `purchase_atomic` — all three writes share one `transaction.atomic()` block,
  with `select_for_update()` on the wallet and product so it is also correct
  under concurrent access. A forced failure raises, the transaction rolls back,
  and **none** of the writes survive.

Production path: `orders/views.py::CheckoutView.post` already wraps payment +
stock + order creation in `@transaction.atomic` — this requirement formalises and
proves that guarantee.

### Before vs After (measured)
A purchase is forced to fail at the order step (wallet starts at 1000, item costs
100):

| Metric | BEFORE (no transaction) | AFTER (atomic) |
|--------|------------------------:|---------------:|
| Money lost on the failed purchase | **100.00** | **0.00** |
| Stock lost on the failed purchase | **1** | **0** |
| Final state | DATA CORRUPTED | fully rolled back |

Under **10 concurrent** failing purchases the atomic version still loses 0 money
and 0 stock — atomicity holds regardless of interleaving.

**Impact:** without the transaction, a failure leaves money debited and stock
gone. With `transaction.atomic()`, a failed purchase leaves the database exactly
as it started — all-or-nothing.

---

## 4. Requirement 5 — Load Distribution (REAL multiple instances)

> **Important — what "instance/node" means.** Per the course feedback, an
> instance is a **separate operating-system process** running the same
> application on its **own port** (8001, 8002, 8003) — **NOT** a thread pool
> inside one process. This section describes that real-process implementation.
> (An earlier in-process `threading`-based simulation lived in
> `core/load_balancer.py`; it is kept only as reference and is **not** the
> answer to this requirement.)

### The problem
A single application instance (one process) is one machine's worth of capacity.
The way to serve more traffic is to run **more instances** and spread requests
across them — horizontal scale-out — instead of overloading the one process.

### The solution — real processes + an HTTP load balancer
| Piece | File | Role |
|-------|------|------|
| Instance identity | `core/instance_info.py` | Reads `INSTANCE_PORT`/PID so each process knows who it is. |
| `/api/whoami/` | `core/views.py` | Every response says which instance served it (and can do a real cached read). |
| Launcher | `scripts/start_instances.py` | Starts N `runserver` **processes**, each on its own port. |
| `HttpLoadBalancer` | `core/http_load_balancer.py` | Forwards real HTTP requests to the instances by a routing strategy. |
| Demo | `orders/management/commands/demo_load_distribution_real.py` | Fires real HTTP load and tallies requests per instance. |

Routing strategies: `round_robin` (cycle through instances; stateless; even when
costs are uniform), `random` (uniform pick; lock-free), `least_connections`
(send to the least-busy instance; adapts to uneven cost, at the price of a
shared mutex-guarded counter).

### How to run it
```bash
# Terminal 1 — start the real instances (separate processes / ports):
python scripts/start_instances.py --ports 8001 8002 8003

# Terminal 2 — drive real HTTP load through the balancer:
python manage.py demo_load_distribution_real --requests 300 --concurrency 20
```

### Before vs After (measured, real HTTP)
300 real HTTP requests. **BEFORE** = all sent to ONE instance; **AFTER** =
balanced across THREE instances (round-robin):

| Setup | Instances used | Requests per instance | Spread |
|-------|---------------:|-----------------------|-------:|
| BEFORE — one instance | 1 | `instance-8001: 300` | — |
| AFTER — 3 instances (round_robin) | 3 | `8001: 100, 8002: 100, 8003: 100` | **0** |

Strategy comparison (same workload): `round_robin` gave the most even split
(spread 0), `least_connections` and `random` showed larger spread under uniform
cost. The per-instance counts come from the instances' own `/api/whoami/`
responses, so the distribution is measured from real traffic, not assumed.

**Strategy chosen & justification:** `round_robin` — with uniform request cost it
splits traffic perfectly evenly (100/100/100) while needing **no shared state**,
keeping routing lock-free. `least_connections` is the right choice when request
costs vary widely (it steers to the least-busy instance) but reads a shared,
mutex-guarded counter on every decision.

**Impact:** one process went from handling 100% of the traffic to three
processes each handling a third — the load is genuinely distributed across real
instances on different ports.

---

## 5. Synchronization points (thread-safety summary)

Per the brief, each critical section is commented at its synchronization point.
Summary of where mutual exclusion is enforced:

| Concern | Primitive | Location |
|---------|-----------|----------|
| Cross-process resource update (Req 7) | Redis `SET NX PX` + Lua release | `core/distributed_lock.py` |
| Composite write atomicity (Req 8) | `transaction.atomic()` + `select_for_update()` | `orders/services.py::purchase_atomic` |
| Load balancer routing state (Req 5) | `threading.Lock` | `core/http_load_balancer.py::_pick` |
| Perf sample aggregation (AOP) | `threading.Lock` | `core/aop.py::PerfCollector` |

---

## 6. How to reproduce every number in this document

```bash
# Optional — start real Redis so Req 7 uses a TRUE distributed lock:
docker run --rm -p 6379:6379 redis

# Req 5 — REAL load distribution across instances (two terminals):
python scripts/start_instances.py --ports 8001 8002 8003   # terminal 1
python manage.py demo_load_distribution_real               # terminal 2 → reports/req5_..._real_*.md

python manage.py demo_distributed_lock         # Req 7  → reports/req7_*.md
python manage.py demo_transaction_integrity    # Req 8  → reports/req8_*.md
```

Each command prints a BEFORE vs AFTER table to the console and saves a timestamped
Markdown report under `reports/`. They accept flags (`--workers`, `--threads`,
`--requests`, `--concurrency`, `--ports`, …) to vary the load.
