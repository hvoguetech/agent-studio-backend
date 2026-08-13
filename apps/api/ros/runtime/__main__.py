"""CLI: `python -m ros.runtime run` — pull (or load) a RunManifest and drive the workflow.

Examples:
  python -m ros.runtime run --manifest-file manifest.json --input '{"messages":[{"role":"user","content":"hi"}]}'
  python -m ros.runtime run --master-url $ROS_MASTER_URL --token $ROS_RUNTIME_TOKEN \
      --project <pid> --workflow <wid> --input '{...}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ros.runtime", description="Standalone ros runtime.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Compile + run a workflow from a RunManifest.")
    r.add_argument("--manifest-file", help="Load the manifest from a local JSON file (offline).")
    r.add_argument("--master-url", help="Master base URL to pull the manifest from.")
    r.add_argument("--token", help="Run-scoped runtime token (Authorization: Bearer).")
    r.add_argument("--run-id", help="Run id to pull the manifest for (with --master-url).")
    r.add_argument("--input", default="{}", help="Run input as JSON (default '{}').")
    r.add_argument("--thread-id", default="run", help="Checkpoint thread id (resume key).")

    d = sub.add_parser(
        "drive",
        help="Drive an existing run to completion on this VM (trusted-VM: shared DB + relay bus).",
    )
    d.add_argument("--run-id", required=True, help="Run id to drive (reads input/thread from the shared DB).")
    d.add_argument("--tenant", required=True, help="Tenant id (binds RLS for the shared-DB reads).")
    d.add_argument("--project", default=None, help="Project id (optional scope guard).")
    d.add_argument("--public", action="store_true", help="Embed surface: redact operator-only frames (H5).")
    d.add_argument("--resume", action="store_true", help="Resume an interrupted (HITL) run.")
    d.add_argument("--resume-value", default=None, help="HITL resume payload as JSON (with --resume).")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_run(args))
    if args.cmd == "drive":
        return asyncio.run(_drive(args))
    return 2


async def _run(args) -> int:
    from ros.runtime.client import fetch_manifest, load_manifest_file
    from ros.runtime.runner import run

    if args.manifest_file:
        manifest = load_manifest_file(args.manifest_file)
    elif args.master_url and args.run_id:
        manifest = await fetch_manifest(args.master_url, args.token, args.run_id)
    else:
        print("error: pass --manifest-file OR --master-url + --run-id + --token", file=sys.stderr)
        return 2

    thread_id = args.thread_id if args.thread_id != "run" else (args.run_id or "run")
    result = await run(manifest, json.loads(args.input), thread_id=thread_id)
    print(json.dumps(result, default=str))
    return 0


async def _drive(args) -> int:
    import os

    from ros.runtime.driver import drive_run

    resume_value = json.loads(args.resume_value) if args.resume_value else None
    # Per-run context is passed as a JSON env by the dispatcher (avoids shell-quoting in the command).
    rc_raw = os.environ.get("ROS_RUN_CONTEXT")
    run_context = json.loads(rc_raw) if rc_raw else None
    await drive_run(
        run_id=args.run_id, tenant_id=args.tenant, project_id=args.project,
        public=args.public, run_context=run_context,
        resume=args.resume, resume_value=resume_value,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
