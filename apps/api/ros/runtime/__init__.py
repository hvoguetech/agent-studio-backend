"""Standalone ros runtime — the execution engine packaged to run OUTSIDE master (on a Freestyle VM).

Fetches a RunManifest from master (services/runtime_manifest.py), rebuilds a CompileContext without
the master DB (services/runtime.build_compile_context_from_manifest), compiles the workflow, and
drives it against the injected durable state. Entry point: `python -m ros.runtime run`.
"""
