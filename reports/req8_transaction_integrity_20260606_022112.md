# Requirement 8 — Transaction Integrity (ACID)

**Generated:** 2026-06-06T02:21:12

## Scenario
A purchase performs three writes — **charge wallet**, **decrement stock**, **create order** — and we force the third to fail. The question is what the database looks like afterwards.

## Single-thread: does a forced failure corrupt state?

| Mode | Money lost | Stock lost | State after failed purchase |
|------|-----------:|-----------:|-----------------------------|
| **NON-ATOMIC** | 100.00 | 1 | DATA CORRUPTED |
| **ATOMIC** | 0.00 | 0 | OK — fully rolled back |

### Before vs After

| Metric | BEFORE (no transaction) | AFTER (atomic) |
|--------|------------------------:|---------------:|
| Money lost on a failed purchase | **100.00** | **0.00** |
| Stock lost on a failed purchase | **1** | **0** |
| Final state | DATA CORRUPTED | OK — fully rolled back |

**Verdict:** without a transaction the wallet was debited (100.00) and stock reduced (1) even though the purchase failed — money taken, no order. Wrapping the three writes in `transaction.atomic()` rolls them all back, so a failed purchase leaves **0.00** money and **0** stock lost.

## Concurrent: atomicity under 20 simultaneous failing purchases

| Mode | Threads | Money lost | Stock lost | Verdict |
|------|--------:|-----------:|-----------:|---------|
| **NON-ATOMIC** | 20 | 200.00 | 4 | DATA CORRUPTED |
| **ATOMIC** | 20 | 0.00 | 0 | OK — fully rolled back |

Atomicity holds even under concurrent access: every atomic attempt either commits fully or rolls back fully, so no partial writes survive regardless of interleaving (the **A** and **C** in ACID, under simultaneous load).

## Where it lives in the codebase
- Atomic service: `orders/services.py::purchase_atomic`
- Broken baseline: `orders/services.py::purchase_non_atomic`
- Production path: `orders/views.py::CheckoutView.post` (`@transaction.atomic` wraps payment + stock + order rows).
