# High-Performance E-Commerce Backend Engine

A Django REST Framework backend built for the Parallel Programming course (2026).
It exposes a small e-commerce API (users, products, cart, checkout, coupons) and
focuses on non-functional requirements: serving many concurrent requests safely,
protecting shared data from corruption, controlling resource usage, and moving
slow work off the request path.

## Features implemented

1. Concurrent access and data integrity: stock and coupon updates use
   pessimistic row locks (`select_for_update`) inside transactions, which
   prevents lost-update race conditions.
2. Resource and capacity control: a bounded semaphore caps how many checkouts
   run at the same time, and per-user rate limiting protects the API endpoints.
3. Asynchronous queues: invoices and notifications are pushed to a database
   backed task queue and processed by a separate worker, so the user is not
   blocked while they run.
4. Batch processing: a management command aggregates daily sales in fixed-size
   chunks, each committed in its own atomic transaction.
5. Load distribution: the same app runs as several **real instances** (separate
   processes on different ports) behind an HTTP load balancer
   (round-robin / random / least-connections); traffic is genuinely spread
   across the instances, removing the single-instance bottleneck.
6. Distributed caching: hot products are served from a Redis cache (cache-aside,
   with an in-process fallback), cutting direct database queries by ~98% and
   invalidated on write to stay consistent across nodes.
7. Concurrency control with a **distributed lock**: a Redis `SET NX PX` lock
   (with a safe Lua release and an in-process fallback) serializes shared-
   resource updates across processes — not just within one.
8. Transaction integrity (ACID): the composite purchase (charge + stock +
   order) is all-or-nothing via `transaction.atomic()`, even under concurrency.
9. Stress / stability testing: a management command fires 100+ concurrent
   checkouts at the real API and verifies the system serves them all with **no
   crash and no data loss** (no overselling, no lost writes).
10. Benchmarking & bottleneck analysis: a management command measures the
    catalogue-listing response time, pinpoints the N+1 query bottleneck, and
    reports a before/after comparison of the `select_related` fix.

Cross-cutting logging and timing are handled by an AOP-style decorator layer
(`core/aop.py`), including a `@measure` performance aspect.

See `docs/REQ_5_7_8_DESIGN.md` and `docs/REQ_6_DESIGN.md` for the design, AOP
usage, and before/after results of requirements 5, 6, 7 and 8. Run the demos:

```bash
# Req 5 — REAL load distribution across instances (two terminals):
python scripts/start_instances.py --ports 8001 8002 8003   # terminal 1: start the instances
python manage.py demo_load_distribution_real               # terminal 2: drive load through the balancer

python manage.py demo_distributed_cache        # Req 6  (start Redis for a real distributed cache)
python manage.py demo_distributed_lock         # Req 7  (start Redis for the real lock)
python manage.py demo_transaction_integrity    # Req 8
```

> **Note on the database:** the dev database is SQLite, tuned for concurrent
> writes in `config/settings.py` (`transaction_mode="IMMEDIATE"`, WAL journal,
> 30s busy timeout) so the stress test can sustain 100 simultaneous writers.
> On PostgreSQL (see `requirements.txt`) the `select_for_update()` row locks
> work natively and this tuning is unnecessary.

## Getting started

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate         

# 2. Install dependencies
pip install -r requirements.txt

# 3. Apply database migrations
python manage.py migrate

# 4. Run the development server
python manage.py runserver
```

To process the asynchronous task queue, run the worker in a second terminal:

```bash
python manage.py run_worker
```

## Stress test & benchmark (Requirements #9 and #10)

No running server is needed — both commands drive the API in-process and write
a timestamped Markdown report to `reports/`.

```bash
# Requirement #9 — 100 concurrent users, proves no crash / no data loss
python manage.py stress_test                 # default 100 users
python manage.py stress_test --users 200     # push it harder

# Requirement #10 — measure latency, expose the N+1 bottleneck, before/after
python manage.py benchmark                    # 200 products, 30 reps
python manage.py benchmark --products 1000
```

## Configuration

Copy `.env.example` to `.env` and set the values you need, for example
`SECRET_KEY` and `DEBUG`.

## Note

This is an academic project for the Parallel Programming course.
