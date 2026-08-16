"""Export an agent's provisioned resource env into the runtime PROCESS (per-end-user isolation 2b).

VM-ONLY. Called from the single-run runtime entrypoint (`ros.runtime.__main__`) so the agent's own
code/tools that read standard vars (DATABASE_URL, REDIS_URL, endpoint URLs, …) reach the resources
THIS run's governed subject provisioned — scoped to (agent, end_user) by `resolved_runtime_env`.

Never call this on shared master: master serves many tenants/users concurrently, so a global
os.environ write there would leak one run's credentials into another. On the VM the process runs
exactly one run, so the write is safe.

Belt-and-suspenders for a REUSED warm/sticky VM process: the keys set on the previous call are
tracked and reconciled each time — a prior run's exported vars not present in this run's set are
removed, and overlapping vars are overwritten — so creds can't bleed across runs on the same VM.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("ros.runtime.env")

# Vars this module set on the LAST apply, so a reused process can be reconciled to the current run.
_exported: set[str] = set()


def apply_runtime_env(env: dict[str, str] | None) -> list[str]:
    """Set `env` (var -> resolved value) on os.environ for this run and return the names applied.

    Reconciles against the previous apply (see module docstring). Non-str values are skipped.
    """
    global _exported
    clean = {k: v for k, v in (env or {}).items() if isinstance(k, str) and isinstance(v, str)}
    # Drop vars a PREVIOUS run exported that this run does not set (warm-VM reconcile).
    for stale in _exported - set(clean):
        os.environ.pop(stale, None)
    for k, v in clean.items():
        os.environ[k] = v
    _exported = set(clean)
    if clean:
        log.info("applied %d provisioned runtime env var(s): %s", len(clean), ",".join(sorted(clean)))
    return sorted(clean)
