"""Standalone ros runtime — the execution engine packaged to run OUTSIDE master (on a Freestyle VM).

TWO VM modes, by isolation level:

1. Trusted-VM, direct-DB drive (DEFAULT / streaming) — `python -m ros.runtime drive` (runtime/driver.py).
   The VM holds shared DB + Redis + secret-key creds (injected at provision) and runs the SAME
   RunService._drive as master: it reads the run/workflow/resolved secrets straight from the shared
   DB, streams frames to the relay bus (master relays the SSE), and finalizes the run row. This is
   what FreestyleBackend dispatches (execution/freestyle_control.dispatch_run). See memory
   `interactive-on-vm-relay`.

2. DB-less manifest-pull (STRICT isolation, non-streaming) — `python -m ros.runtime run`
   (runtime/runner.py `run`). The VM never touches the master DB: it fetches a RunManifest over a
   run-scoped token (routers/runtime.py, services/runtime_manifest.py), rebuilds a CompileContext
   without the DB (services/runtime.build_compile_context_from_manifest), and drives via ainvoke.
   Retained as the harder-isolation option; NOT the path FreestyleBackend uses today.
"""
