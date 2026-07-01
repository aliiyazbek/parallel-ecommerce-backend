# Presentation Guide — Requirements 5 to 10

A quick reference for the evaluation. For each requirement:
**(1) How to run · (2) What we did (problem → fix → which API) · (3) Where it is in code.**

> **Prerequisite:** Redis is running (Memurai service on `localhost:6379`). Check with
> `Get-Service Memurai`. It starts automatically with Windows.

---

## Requirement 5 — Load Distribution

### 1. How to run (two terminals)
```powershell
# Terminal 1 — start 3 real instances (separate processes on different ports):
python scripts/start_instances.py --ports 8001 8002 8003

# Terminal 2 — drive load through the balancer (before/after + report):
python manage.py demo_load_distribution_real
```

### 2. What we did
- **Problem:** one API instance is a single process on one port with a fixed worker
  capacity. Under heavy traffic it saturates and rejects the overflow — the
  *single-instance bottleneck*.
- **Fix:** run the **same app as several real instances** (separate OS processes on
  ports 8001/8002/8003) behind a **load balancer** that distributes requests by a
  strategy (round-robin). An instance = a **process**, not a thread.
- **Which API:** `GET /api/whoami/` — each instance returns its name/port/PID, so we
  measure exactly which instance served each request.
- **Result:** 1 instance handled 300/300 requests → 3 instances handled 100/100/100
  (spread = 0, perfectly even).

### 3. Where it is in code
| Piece | File |
|-------|------|
| Instance launcher (processes/ports) | [scripts/start_instances.py](../scripts/start_instances.py) |
| Instance identity | [core/instance_info.py](../core/instance_info.py) |
| `/api/whoami/` endpoint | [core/views.py:26](../core/views.py#L26) |
| Load balancer + strategies | [core/http_load_balancer.py:53](../core/http_load_balancer.py#L53) (`_pick`) |
| Demo + report | [orders/management/commands/demo_load_distribution_real.py](../orders/management/commands/demo_load_distribution_real.py) |

**Synchronization point:** `threading.Lock` guarding the round-robin cursor and
in-flight counters — [core/http_load_balancer.py:53](../core/http_load_balancer.py#L53) (`_pick`).

---

## Requirement 6 — Distributed Caching

### 1. How to run
```powershell
# Before/after numbers (1000 reads):
python manage.py demo_distributed_cache --reads 1000 --threads 20

# PROOF the cache is shared across processes (one command, self-contained):
python scripts/demo_shared_cache.py
```

### 2. What we did
- **Problem:** popular products are read thousands of times but barely change. Without
  a cache, **every** read hits the database — the DB becomes the bottleneck.
- **Fix:** **Redis** with the **cache-aside** pattern. First read = MISS (query DB, store
  in Redis); every later read = HIT (served from Redis, no DB query). On write, the
  cache entry is **invalidated** so data stays consistent.
- **Why Redis (distributed):** with multiple instances (Req 5), a local dict per process
  would be inconsistent. Redis is shared by all instances — one view, one place to invalidate.
- **Which API:** product reads via `get_product_cached_with_hit`; also exposed through
  `GET /api/whoami/?product=<id>` and the live `GET /api/products/`.
- **Result:** 1000 reads → DB queries dropped **1000 → 1** (99.9% hit ratio); read
  latency ~25ms → ~0.12ms.

### 3. Where it is in code
| Piece | File |
|-------|------|
| Cache ops (get/set/delete) | [core/cache.py:153](../core/cache.py#L153) |
| Before vs after read | [products/services.py:52](../products/services.py#L52) (`get_product_uncached`) vs [:65](../products/services.py#L65) (`get_product_cached_with_hit`) |
| Invalidation on write | [products/views.py:37](../products/views.py#L37) (`perform_update` / `perform_destroy`) |
| Demo + report | [orders/management/commands/demo_distributed_cache.py](../orders/management/commands/demo_distributed_cache.py) |
| Cross-process proof | [scripts/demo_shared_cache.py](../scripts/demo_shared_cache.py) |

**Synchronization point:** `threading.Lock` on the hit/miss counters and fallback
store — [core/cache.py](../core/cache.py). On real Redis, `SET ... PX` is atomic server-side.

---

## Requirement 7 — Concurrency Control (Distributed Lock)

### 1. How to run
```powershell
python manage.py demo_distributed_lock --workers 50 --stock 10
```
(Runs UNSAFE then SAFE; report shows `backend: redis`.)

### 2. What we did
- **Problem:** updating sensitive stock — if **two separate servers** read the same
  stock, both pass the check, and both write, you **oversell**. A normal `threading.Lock`
  or a DB row lock only protects one process.
- **Fix:** a **distributed lock** in **Redis** (pessimistic). Acquire with
  `SET key token NX PX ttl` (atomic "set if absent" + auto-expiry); release with a Lua
  script that deletes the key only if our token still matches (safe release). Only one
  server anywhere holds the lock for a given product at a time.
- **Which API:** the stock-decrement service `decrement_stock_dlock_safe`; the live
  checkout path `POST /api/checkout/` uses the same locking idea.
- **Result:** oversold **40 units → 0** with the distributed lock.

### 3. Where it is in code
| Piece | File |
|-------|------|
| Distributed lock (acquire/release) | [core/distributed_lock.py:156](../core/distributed_lock.py#L156) (class), [release Lua :65](../core/distributed_lock.py#L65) |
| Before vs after service | [orders/services.py:70](../orders/services.py#L70) (`dlock_unsafe`) vs [:91](../orders/services.py#L91) (`dlock_safe`) |
| Demo + report | [orders/management/commands/demo_distributed_lock.py](../orders/management/commands/demo_distributed_lock.py) |

**Synchronization point:** the distributed lock itself —
`with DistributedLock(f"stock:{id}")` in [orders/services.py:91](../orders/services.py#L91) (`decrement_stock_dlock_safe`).

**Three properties to mention:** mutual exclusion (`NX`), deadlock-freedom (`PX` TTL),
safe release (unique token + Lua compare-and-delete).

---

## Requirement 8 — Transaction Integrity (ACID)

### 1. How to run
```powershell
python manage.py demo_transaction_integrity --threads 20
```

### 2. What we did
- **Problem:** a purchase is **composite** — (1) charge wallet, (2) decrement stock,
  (3) create order. If step 3 fails after 1 & 2, the customer is **charged with no order**
  and stock vanished → corrupted database.
- **Fix:** wrap all three writes in **`transaction.atomic()`** — all-or-nothing. A forced
  failure rolls everything back, so nothing partial survives, even under concurrency.
- **Which API:** the purchase service `purchase_atomic`; the production path
  `POST /api/checkout/` is wrapped in `@transaction.atomic` (payment + stock + order).
- **Result:** on a failed purchase, money lost **100 → 0**, stock lost **1 → 0**; under
  20 concurrent failing purchases, still 0 lost.

### 3. Where it is in code
| Piece | File |
|-------|------|
| Atomic vs broken purchase | [orders/services.py:129](../orders/services.py#L129) (`purchase_non_atomic`) vs [:156](../orders/services.py#L156) (`purchase_atomic`) |
| Production checkout | [orders/views.py:101](../orders/views.py#L101) (`@transaction.atomic` on `CheckoutView.post`) |
| Demo + report | [orders/management/commands/demo_transaction_integrity.py](../orders/management/commands/demo_transaction_integrity.py) |

**Synchronization point:** `transaction.atomic()` + `select_for_update()` on wallet and
product — [orders/services.py:156](../orders/services.py#L156) (`purchase_atomic`).

---

## Requirement 9 — Stress / Stability Testing

### 1. How to run
```powershell
# In-process, full DRF stack — sequential vs concurrent:
python manage.py stress_test --mode both

# External HTTP load (like JMeter/Locust), against the live server:
python manage.py runserver 127.0.0.1:8000          # terminal 1
python scripts/load_test.py --base-url http://127.0.0.1:8000 --mode both   # terminal 2
```

### 2. What we did
- **Problem:** prove the system serves **≥100 concurrent users** without crashing or
  losing data.
- **Fix:** two tools. `stress_test` fires N concurrent (or sequential) checkouts at the
  real `/api/checkout/` endpoint and verifies integrity directly against the DB.
  `load_test.py` is an **external HTTP client** hitting the live server over the network.
- **Which API:** `POST /api/checkout/` (the heaviest write path, contends on the stock row).
- **Metrics reported:** Total Requests · Success · Failed · **Average Response Time** ·
  System crashed (yes/no).
- **Result (100 users):** sequential avg 10ms, concurrent avg 660ms — 100/100 success,
  0 failed, 0 crash, 0 oversold in both.

### 3. Where it is in code
| Piece | File |
|-------|------|
| In-process stress test (Barrier) | [orders/management/commands/stress_test.py:138](../orders/management/commands/stress_test.py#L138) (`run_concurrent`) / [:163](../orders/management/commands/stress_test.py#L163) (`run_sequential`) |
| External HTTP load tester | [scripts/load_test.py](../scripts/load_test.py) |
| Endpoint under test | [orders/views.py:102](../orders/views.py#L102) (`CheckoutView.post`) |

**Synchronization point:** stresses the lock + atomic transaction in checkout (Req 1/7/8).

---

## Requirement 10 — Benchmarking & Bottleneck Analysis

### 1. How to run
```powershell
python manage.py benchmark --products 200 --repeat 30
```

### 2. What we did
- **Problem:** measure the most-hit operation, find a bottleneck, optimize, and prove the
  gain with real numbers (before/after).
- **Fix:** measured `GET /api/products/` (the catalogue listing). Found the **N+1 query**
  bottleneck — the serializer reads `category.name`, so listing N products without
  `select_related` fired N extra queries. Added **`select_related("category")`**, which
  joins the category in one query.
- **Which API:** `GET /api/products/`.
- **Result:** DB queries **201 → 1** (100% fewer), mean latency **53ms → 9ms**
  (~6× faster).

### 3. Where it is in code
| Piece | File |
|-------|------|
| The fix (applied to the live API) | [products/views.py:26](../products/views.py#L26) (`ProductViewSet.queryset`, `select_related`) |
| Benchmark harness (counts queries) | [orders/management/commands/benchmark.py:82](../orders/management/commands/benchmark.py#L82) (`measure_serialization`) |

**AOP:** production timing comes from `@log_execution` in `core/aop.py`; the benchmark
uses `CaptureQueriesContext` to count queries before/after.

---

## Synchronization Points — precise map (all clickable)

A *synchronization point* is where multiple threads/processes touch shared data, so
access must be serialized to avoid a race condition. The project uses **5 types**, each
the right tool for its situation.

### 1. Pessimistic DB row lock — `select_for_update()`
Locks a database row until the transaction commits; other transactions touching that row
**block and wait**. Serializes the stock read-check-write. *(Pessimistic, inside one DB.)*

| Use | Location |
|-----|----------|
| Stock decrement (Req 1) | [orders/services.py:34](../orders/services.py#L34) (`decrement_stock_safe`) |
| Checkout — locks every cart product (Req 1/8) | [orders/views.py:121](../orders/views.py#L121) |
| Purchase — locks wallet + product (Req 8) | [orders/services.py:158](../orders/services.py#L158) (`purchase_atomic`) |

### 2. Distributed lock — Redis `SET NX PX` + Lua release
Only one server **anywhere** can hold the key (`NX` atomic), auto-expires (`PX`), released
with a Lua compare-and-delete. Guards the stock update **across processes**. *(Pessimistic,
cross-process — Req 7.)*

| Use | Location |
|-----|----------|
| Acquire (`SET NX PX`) | [core/distributed_lock.py:213](../core/distributed_lock.py#L213) |
| Safe release (Lua) | [core/distributed_lock.py:248](../core/distributed_lock.py#L248) (script at [:65](../core/distributed_lock.py#L65)) |
| Applied to stock update | [orders/services.py:92](../orders/services.py#L92) (`decrement_stock_dlock_safe`) |

### 3. Bounded semaphore (bulkhead) — `threading.BoundedSemaphore`
Caps how many threads run the critical section at once (e.g. 3 checkouts); excess wait or
get HTTP 503. Released in `finally` so no permit leaks. *(Admission control — Req 2.)*

| Use | Location |
|-----|----------|
| `limit_concurrency` semaphore | [core/concurrency.py:16](../core/concurrency.py#L16) |
| Applied to checkout | [orders/views.py:91](../orders/views.py#L91) |

### 4. In-memory mutex — `threading.Lock`
Makes a non-atomic operation (`counter += 1`, advancing a cursor) safe for one thread at a
time. Guards shared in-memory state. *(Mutex, within one process — Req 5/6 + AOP.)*

| Use | Location |
|-----|----------|
| Round-robin cursor + in-flight counters (Req 5) | [core/http_load_balancer.py:51](../core/http_load_balancer.py#L51) (`_pick` at [:53](../core/http_load_balancer.py#L53)) |
| Cache hit/miss counters (Req 6) | [core/cache.py:63](../core/cache.py#L63) |
| Cache fallback store (Req 6) | [core/cache.py:93](../core/cache.py#L93) |
| AOP perf collector | [core/aop.py:62](../core/aop.py#L62) |

### 5. Worker task-claim — `select_for_update(skip_locked=True)`
Each worker locks only the task rows it claims and **skips** rows another worker locked, so
parallel workers pull disjoint batches and no task runs twice. *(Pessimistic + skip — Req 3.)*

| Use | Location |
|-----|----------|
| Atomic batch claim | [notifications/worker.py:17](../notifications/worker.py#L17) |
| Batch upsert lock (Req 4, `skip_locked=False` = block) | [orders/management/commands/process_daily_sales.py:194](../orders/management/commands/process_daily_sales.py#L194) |

> **How to explain the choice:** "I use a lock only where there is genuinely shared mutable
> state, and I pick the tool by scope — a DB row lock inside one database, a Redis
> distributed lock across processes, a semaphore for admission control, and a `threading.Lock`
> for in-memory counters. Locking everything would just add contention and slow the system."

---

## One-line cheat sheet for the meeting

| Req | One sentence |
|-----|--------------|
| 5 | "Same app as 3 real processes on different ports, a load balancer spreads requests — 1 instance → 100/100/100." |
| 6 | "Redis cache-aside in front of the DB — 1000 reads dropped from 1000 DB queries to 1; shared across all instances." |
| 7 | "Redis distributed pessimistic lock (`SET NX PX` + Lua release) — overselling 40 → 0 across processes." |
| 8 | "All three purchase writes in one `transaction.atomic()` — a failed purchase loses 0 money and 0 stock." |
| 9 | "100 concurrent checkouts at the real endpoint — 100% success, 0 crash, 0 data loss; plus an external HTTP load tool." |
| 10 | "Found the N+1 bottleneck in product listing, fixed with `select_related` — 201 queries → 1, ~6× faster." |
