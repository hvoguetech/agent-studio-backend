/**
 * ros-freestyle-svc config.
 *
 * The run-control service ROS's `freestyle` ExecutionBackend dispatches to (see
 * apps/api/ros/execution/freestyle_control.py). It boots the ros runtime on a Freestyle VM.
 *
 * VM lifecycle policy (docs/GAPS.md G2): a VM runs until it is EXPLICITLY torn down. We create VMs
 * with `persistence: "persistent"` and NO idle timeout by default, so nothing auto-suspends or
 * recycles them; only `DELETE /vm/:id` destroys a VM. Configurable via env for later policy work.
 */
function req(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}
function opt(name: string, fallback = ""): string {
  return process.env[name] ?? fallback;
}

export const config = {
  // The Freestyle API key lives ONLY here (control plane) — never injected into a run VM.
  freestyleApiKey: req("FREESTYLE_API_KEY"),
  // Shared secret ROS presents as `Authorization: Bearer <secret>`. Must equal ROS's
  // ROS_FREESTYLE_SERVICE_SECRET. The service runs private (no public domain); this defends against
  // anything reaching it on the private network without the secret.
  serviceSecret: req("FREESTYLE_SERVICE_SECRET"),
  port: Number(opt("PORT", "3000")),

  // VM persistence at create time. "persistent" => runs until explicit teardown (the chosen policy);
  // "sticky" => suspend-on-idle/resume-on-access (NOT our policy, kept for later experiments);
  // "ephemeral" => dies on idle. Default persistent.
  persistence: opt("ROS_VM_PERSISTENCE", "persistent") as "persistent" | "sticky" | "ephemeral",
  stickyPriority: Number(opt("ROS_VM_STICKY_PRIORITY", "1")),
  // Idle timeout (seconds). 0/empty => no idle timeout (never auto-suspends) — the default for the
  // persistent policy. Only meaningful for sticky/ephemeral.
  idleTimeoutSeconds: Number(opt("ROS_VM_IDLE_TIMEOUT_SECONDS", "0")),

  // Prebaked Freestyle snapshot image with Python + the `ros` package (+ claude CLI/Node) baked in,
  // so a VM can run `python -m ros.runtime` immediately. Empty => boot a base python VM (the runtime
  // must be otherwise available). Baking this snapshot is a follow-up (spec Part G / GAPS G2).
  snapshotId: opt("ROS_SNAPSHOT_ID", ""),

  // Per-exec timeout (ms) for launching the runtime command on the VM. The runtime itself runs
  // DETACHED (the run drives against the shared DB + relay bus), so this only bounds the launch.
  execTimeoutMs: Number(opt("ROS_VM_EXEC_TIMEOUT_MS", String(60_000))),
};
