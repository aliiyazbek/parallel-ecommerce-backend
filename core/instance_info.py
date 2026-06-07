"""Per-instance identity (Requirement #5 — Load Distribution).

Each running copy of this Django application is a SEPARATE OS PROCESS listening
on its OWN PORT (e.g. 8001, 8002, 8003). That is what "instance / node" means
here — a process, not a thread. This module lets every request report *which*
instance served it, so the load balancer's distribution is observable.

The port is passed to each instance via the `INSTANCE_PORT` environment variable
when it is launched (see `scripts/start_instances.py`); the PID is the OS process
id, which is naturally different for every instance.
"""

from __future__ import annotations

import os

# Set per process when the instance is launched. Falls back to the dev default.
INSTANCE_PORT = os.getenv("INSTANCE_PORT", "8000")
# A human-friendly name, also settable per instance; defaults to the port.
INSTANCE_NAME = os.getenv("INSTANCE_NAME", f"instance-{INSTANCE_PORT}")


def instance_info() -> dict:
    """Identity of THIS process: its name, port and OS PID."""
    return {
        "instance": INSTANCE_NAME,
        "port": INSTANCE_PORT,
        "pid": os.getpid(),
    }
