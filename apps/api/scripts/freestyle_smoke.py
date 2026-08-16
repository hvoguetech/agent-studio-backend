"""Freestyle + Redis live-verify smoke harness (ROS #4).

The interactive-on-VM path is unit-tested only with in-memory doubles; this exercises the two
infra-dependent pieces against REAL services so the "runs for real" claim can be made:

  preflight     — validate the env a VM run needs (freestyle-svc, shared Redis, checkpoint, master
                  URL) and print a readiness report. Never connects; safe to run anywhere.
  redis-relay   — exercise the REAL RedisRelayBus against $ROS_REDIS_URL: publish frames (incl. a
                  terminal), assert the LPUSH/LTRIM buffer replays them in order, and assert a live
                  subscriber receives them. This is the RedisRelayBus smoke from #4.

The remaining #4 smokes (dispatch an interactive run end-to-end, cancel mid-run, kill the VM →
watchdog) need a real VM + a seeded workflow and are driven from the console/API — see
docs/runbooks/freestyle-live-verify.md.

Usage:
  ./.venv312/bin/python -m scripts.freestyle_smoke preflight
  ROS_REDIS_URL=rediss://... ./.venv312/bin/python -m scripts.freestyle_smoke redis-relay
  ./.venv312/bin/python -m scripts.freestyle_smoke redis-relay --dry-run   # in-memory bus (logic check)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ros.config import settings


def _ok(b: bool) -> str:
    return "OK  " if b else "MISS"


def preflight() -> int:
    """Report whether the env a Freestyle VM run needs is present. Returns non-zero if a REQUIRED
    piece is missing."""
    fs_url = settings.freestyle_service_url or ""
    redis_url = settings.redis_url or ""
    ckpt_backend = (settings.checkpoint_backend or "").lower()
    ckpt_url = settings.checkpoint_postgres_url or settings.database_url or ""
    master_url = settings.public_base_url or ""

    required = {
        "ROS_FREESTYLE_SERVICE_URL (freestyle-svc)": bool(fs_url),
        "ROS_REDIS_URL (shared bus)": bool(redis_url),
        "master URL (ROS_PUBLIC_BASE_URL, for the VM to pull the manifest)": bool(master_url),
        "checkpoint backend = postgres (durable, master-visible)": ckpt_backend == "postgres",
        "checkpoint/DB URL": bool(ckpt_url),
    }
    print("Freestyle live-verify preflight")
    print("-" * 60)
    for label, present in required.items():
        print(f"  [{_ok(present)}] {label}")

    # Advisories — not fatal, but wrong values are the usual first-deploy failures.
    print("\nAdvisories")
    print("-" * 60)
    # The VM is OUTSIDE Railway's private net, so it needs a public TLS Redis with AUTH.
    tls = redis_url.startswith("rediss://")
    has_auth = "@" in redis_url.split("//", 1)[-1] if redis_url else False
    print(f"  [{_ok(tls)}] Redis is public TLS (rediss://) — required for the off-net VM"
          + ("" if tls else f"  (got: {redis_url.split('://',1)[0] + '://…' if redis_url else 'unset'})"))
    print(f"  [{_ok(has_auth)}] Redis URL carries AUTH credentials")
    print(f"  [{'ON ' if settings.freestyle_warm_vms else 'off'}] warm/sticky VMs (ROS_FREESTYLE_WARM_VMS)")

    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"\nNOT READY — {len(missing)} required item(s) missing.")
        return 1
    print("\nREADY — required env present. Run `redis-relay` next, then the console smokes in the runbook.")
    return 0


async def _redis_relay(dry_run: bool) -> int:
    """Publish a short frame sequence and assert both buffered replay and live subscribe deliver it
    in order and stop at the terminal frame — the RedisRelayBus contract, against real Redis."""
    from ros.services import run_relay
    from ros.services.run_relay import _channel, publish_frame, relay_frames

    if dry_run:
        run_relay.set_relay_bus(run_relay.InMemoryRelayBus())
        print("redis-relay: DRY RUN (in-memory bus)")
    else:
        if not settings.redis_url:
            print("redis-relay: ROS_REDIS_URL is unset — set it (or pass --dry-run).", file=sys.stderr)
            return 2
        run_relay.set_relay_bus(run_relay.RedisRelayBus(settings.redis_url))
        print(f"redis-relay: LIVE against {settings.redis_url.split('://',1)[0]}://…")

    tenant, run_id = "smoke_t", "smoke_run_1"
    frames = [
        {"event": "run", "data": {"run_id": run_id}},
        {"event": "token", "data": {"chunk": "hello "}},
        {"event": "token", "data": {"chunk": "world"}},
        {"event": "done", "data": {"status": "done"}},  # terminal — relay must stop here
    ]
    # Publish first so buffered replay (LPUSH/LTRIM path) has something to return.
    for i, f in enumerate(frames, start=1):
        await publish_frame(run_id, i, f, tenant_id=tenant)

    # 1) Buffered replay from seq 0 must return all frames in order, stopping at the terminal.
    replayed = [(seq, fr["event"]) async for seq, fr in relay_frames(run_id, 0, tenant_id=tenant)]
    expected = [(1, "run"), (2, "token"), (3, "token"), (4, "done")]
    ok_replay = replayed == expected
    print(f"  [{_ok(ok_replay)}] buffered replay in order + stops at terminal: {replayed}")

    # 2) Last-Event-ID replay: from seq 2, only the later frames come back.
    tail = [(seq, fr["event"]) async for seq, fr in relay_frames(run_id, 2, tenant_id=tenant)]
    ok_tail = tail == [(3, "token"), (4, "done")]
    print(f"  [{_ok(ok_tail)}] Last-Event-ID replay skips seen frames: {tail}")

    # 3) Live pub/sub: a subscriber started now, then a fresh terminal publish, must be delivered.
    live_run = "smoke_run_2"
    channel = _channel(live_run, tenant)
    bus = run_relay.get_relay_bus()
    received: list[str] = []

    async def _listen() -> None:
        async for item in bus.subscribe(channel):
            received.append((item.get("frame") or {}).get("event"))
            if (item.get("frame") or {}).get("event") in {"done", "error", "canceled"}:
                return

    task = asyncio.create_task(_listen())
    await asyncio.sleep(0.2)  # let the SUBSCRIBE land before publishing
    await publish_frame(live_run, 1, {"event": "token", "data": {"chunk": "hi"}}, tenant_id=tenant)
    await publish_frame(live_run, 2, {"event": "done", "data": {"status": "done"}}, tenant_id=tenant)
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
    ok_live = received == ["token", "done"]
    print(f"  [{_ok(ok_live)}] live pub/sub delivery: {received}")

    passed = ok_replay and ok_tail and ok_live
    print("\nPASS — RedisRelayBus verified." if passed else "\nFAIL — see misses above.")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="freestyle_smoke", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="Validate the env a VM run needs (no connections).")
    rr = sub.add_parser("redis-relay", help="Exercise RedisRelayBus against $ROS_REDIS_URL.")
    rr.add_argument("--dry-run", action="store_true", help="Use the in-memory bus (validate logic offline).")
    args = parser.parse_args(argv)

    if args.cmd == "preflight":
        return preflight()
    if args.cmd == "redis-relay":
        return asyncio.run(_redis_relay(args.dry_run))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
