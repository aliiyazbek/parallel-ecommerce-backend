# Task 9 — Stress / Stability Testing

**Generated:** 2026-06-18T10:54:21  
**Load mode:** concurrent  
**Users / requests:** 100  
**Requirement:** Serve at least 100 concurrent users without crash or data loss.

## Summary (test-tool style)

| Metric | Value |
|--------|------:|
| Load mode | concurrent |
| Total Requests | 100 |
| Success Requests | 100 |
| Failed Requests | 0 |
| Average Response Time | 921 ms |
| System crashed | NO |

## Scenario
100 distinct users, each with a cart holding 1 unit of the same product, are released **simultaneously** by a `threading.Barrier` and each calls the real `POST /api/checkout/` endpoint through DRF's `APIClient`. The single shared product-stock row is the contended resource, so this stresses authentication, the capacity bulkhead, the atomic transaction, and the serialized stock read-check-write all at once.

> **How integrity is guaranteed across backends:** on **PostgreSQL** the checkout takes a true row-level lock via `select_for_update()`. On the **SQLite** dev database that clause is a no-op, so correctness instead relies on `transaction_mode="IMMEDIATE"` (configured in `settings.py`), which makes every `transaction.atomic()` block acquire the write lock at `BEGIN` — fully serializing the read-check-write and giving the same no-oversell guarantee the numbers below confirm.

## Throughput & latency

| Users | Wall (ms) | Throughput (req/s) | avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |
|------:|----------:|-------------------:|---------:|---------:|---------:|---------:|---------:|
| 100 | 1812 | 55.2 | 921 | 925 | 1644 | 1703 | 1728 |

## Response breakdown

| 201 Created | 503 Load-shed | 400 Out-of-stock | 429 Throttled | 5xx Errors | Exceptions |
|------------:|--------------:|-----------------:|--------------:|-----------:|-----------:|
| 100 | 0 | 0 | 0 | 0 | 0 |

> **503** responses come from the bounded-concurrency bulkhead (`@limit_concurrency`, Requirement #2). They are *graceful load-shedding*, not crashes, and never correspond to a partial/lost write.

## Data-integrity verification (no data loss)

| Metric | Value |
|--------|------:|
| Initial stock | 100 |
| Final stock | 0 |
| Units sold (initial − final) | 100 |
| Successful checkouts (HTTP 201) | 100 |
| Orders persisted in DB | 100 |
| Oversold by | 0 |

| Check | Result |
|-------|:------:|
| No lost writes (201s == orders in DB) | ✅ PASS |
| Stock consistent (units sold == checkouts) | ✅ PASS |
| No overselling (oversold == 0, stock ≥ 0) | ✅ PASS |
| No crash (zero 5xx / exceptions) | ✅ PASS |

## Verdict: ✅ PASSED

The system served **100 concurrent users** with **100 successful orders**, **0 oversold units**, and **0 lost writes or crashes** — meeting Requirement #9.

## Where it lives in the codebase
- Test harness: `orders/management/commands/stress_test.py`
- Endpoint under test: `orders/views.py::CheckoutView.post`
- Integrity primitives exercised: pessimistic lock (`select_for_update`) + `transaction.atomic` + `@limit_concurrency`.
