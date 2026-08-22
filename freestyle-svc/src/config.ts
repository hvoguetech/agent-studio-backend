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
  // Which execution provider backs the endpoints: "freestyle" (default) | "northflank". The HTTP
  // contract is identical; only the adapter differs. ROS's ExecutionBackend is unaware of this.
  provider: opt("EXECUTION_PROVIDER", "freestyle") as "freestyle" | "northflank",

  // The Freestyle API key lives ONLY here (control plane) — never injected into a run VM. Optional
  // now: required only when provider=freestyle (validated by the Freestyle adapter's enabled()).
  freestyleApiKey: opt("FREESTYLE_API_KEY"),
  // Shared secret ROS presents as `Authorization: Bearer <secret>`. Must equal ROS's
  // ROS_FREESTYLE_SERVICE_SECRET. The service runs private (no public domain); this defends against
  // anything reaching it on the private network without the secret.
  serviceSecret: req("FREESTYLE_SERVICE_SECRET"),
  port: Number(opt("PORT", "3000")),

  // --- Northflank (used only when provider=northflank) -------------------------------------------
  // API token (Team or Org scoped) the control plane uses to create/exec/delete services. Lives
  // ONLY here — never injected into a run container. Required only for provider=northflank.
  northflankApiToken: opt("NORTHFLANK_API_TOKEN"),
  // Project the run Services are created in. Northflank services live under a project.
  northflankProjectId: opt("NORTHFLANK_PROJECT_ID"),
  // How the run Service gets its image. Two modes:
  //  - INTERNAL build (recommended): the image built by a Northflank build/combined service in this
  //    project. Set NORTHFLANK_BUILD_SERVICE_ID (the build service id, e.g. "build-service") + branch
  //    + SHA ("latest" = most recent build). The run Service deploys deployment.internal.
  //  - EXTERNAL image: a container-registry image path. Set NORTHFLANK_RUN_IMAGE. Used when
  //    NORTHFLANK_BUILD_SERVICE_ID is unset.
  northflankBuildServiceId: opt("NORTHFLANK_BUILD_SERVICE_ID"),
  northflankBuildBranch: opt("NORTHFLANK_BUILD_BRANCH", "main"),
  northflankBuildSha: opt("NORTHFLANK_BUILD_SHA", "latest"),
  northflankImage: opt("NORTHFLANK_RUN_IMAGE"),
  // Region/plan for the run Service (provider defaults apply when unset).
  northflankRegion: opt("NORTHFLANK_REGION"),
  northflankPlan: opt("NORTHFLANK_PLAN", "nf-compute-20"),
  // Idle TTL (seconds) after which a warm run Service may be reaped. 0 = keep until explicit delete.
  northflankIdleTtlSeconds: Number(opt("NORTHFLANK_IDLE_TTL_SECONDS", "0")),

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
