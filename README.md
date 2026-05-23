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

Cross-cutting logging and timing are handled by an AOP-style decorator layer.

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

## Configuration

Copy `.env.example` to `.env` and set the values you need, for example
`SECRET_KEY` and `DEBUG`.

## Note

This is an academic project for the Parallel Programming course.
