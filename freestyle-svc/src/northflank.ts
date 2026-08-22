/**
 * Northflank execution adapter — the ExecutionProvider used when EXECUTION_PROVIDER=northflank.
 *
 * Structural twin of the Freestyle adapter, mapped onto Northflank primitives:
 *   - Freestyle "persistent microVM per agent + vm.exec"  ==>  a warm Northflank **deployment
 *     Service** per stickyKey (from the prebuilt run image) + **exec.execServiceCommand**.
 *   - dispatchRun: reuse the live Service for the stickyKey if present, else create one; then exec
 *     the run's command DETACHED (nohup + background) so the call returns a receipt immediately —
 *     the ros runtime drives the run itself against the shared DB + relay bus (trusted model).
 *   - runStatus / teardownVm: get / delete the Service.
 *
 * The receipt's `vmId` carries the Northflank **serviceId** (opaque executor id to ROS).
 *
 * ⚠️ LIVE-VERIFY: the exact @northflank/js-client method shapes (create.service.deployment payload,
 * exec.execServiceCommand, get/delete.service) must be confirmed against the installed client version
 * on first real use — modeled from the Northflank API docs.
 */
import { config } from "./config.js";
import type { ExecutionProvider, RunInput, RunReceipt, RunStatus, ExecutorRecord } from "./provider.js";

// The JS client is a runtime dependency; import lazily so the Freestyle-only deploy needn't load it.
let _client: any = null;
async function nf(): Promise<any> {
  if (_client) return _client;
  const { ApiClient, ApiClientInMemoryContextProvider } = await import("@northflank/js-client");
  const ctx = new ApiClientInMemoryContextProvider();
  await ctx.addContext({ name: "ros", token: config.northflankApiToken });
  _client = new ApiClient(ctx);
  return _client;
}

/** The JS client does NOT throw on HTTP errors by default — it returns them in `res.error`. Unwrap
 * that so a billing/permission/validation failure (e.g. 409 "add a default payment method") surfaces
 * loudly instead of showing up as a mysterious undefined id. Returns `res.data` on success. */
function ok(res: any, what: string): any {
  const err = res?.error;
  if (err) {
    const status = err.status ? `${err.status} ` : "";
    throw new Error(`northflank ${what}: ${status}${err.message ?? "request failed"}`);
  }
  return res?.data;
}

export function northflankEnabled(): boolean {
  return Boolean(config.northflankApiToken && config.northflankProjectId && config.northflankImage);
}

/** In-memory stickyKey -> serviceId (one warm run Service per agent). Best-effort, like the Freestyle
 * map: on restart we re-verify via get.service before reuse; ROS's Run.executor row is the source of
 * truth, so a lost entry just means the next /run creates a fresh Service. */
const svcBySticky = new Map<string, string>();
const records = new Map<string, ExecutorRecord>();

/** A Northflank-safe service name for a run/sticky key (lowercase alnum + dashes, length-capped). */
function serviceName(input: RunInput): string {
  const base = (input.stickyKey || input.runId || "ros-run").toLowerCase().replace(/[^a-z0-9-]/g, "-");
  return ("ros-" + base).slice(0, 60).replace(/-+$/g, "");
}

/** Is a Northflank service still present? A gone/deleted service throws → not alive. */
async function serviceAlive(serviceId: string): Promise<boolean> {
  try {
    const c = await nf();
    await c.get.service({ parameters: { projectId: config.northflankProjectId, serviceId } });
    return true;
  } catch {
    return false;
  }
}

/** Create a warm deployment Service from the prebuilt run image (long-running so we can exec into it).
 * The container idles (the run itself is exec'd in), so we run a trivial keep-alive command. */
async function createService(input: RunInput): Promise<string> {
  const c = await nf();
  const name = serviceName(input);
  const res: any = await c.create.service.deployment({
    parameters: { projectId: config.northflankProjectId },
    data: {
      name,
      billing: { deploymentPlan: config.northflankPlan },
      deployment: {
        instances: 1,
        // Prebuilt image (internal Northflank registry ref or external image).
        external: { imagePath: config.northflankImage },
        // Keep the container alive so we can exec the run into it; the runtime is exec'd, not the CMD.
        docker: { configType: "customCommand", customCommand: "sh -c 'sleep infinity'" },
      },
      ...(config.northflankRegion ? { region: config.northflankRegion } : {}),
    },
  });
  const data = ok(res, "create service");
  const serviceId = data?.id ?? data?.service?.id;
  if (!serviceId) throw new Error("northflank create service: no service id in response");
  return serviceId;
}

/** Resolve the Service to run on: reuse the live sticky Service if present, else create one. */
async function resolveService(input: RunInput): Promise<{ serviceId: string; reused: boolean }> {
  const key = input.stickyKey;
  if (key) {
    const existing = svcBySticky.get(key);
    if (existing && (await serviceAlive(existing))) return { serviceId: existing, reused: true };
    if (existing) svcBySticky.delete(existing); // stale entry — service gone
  }
  const serviceId = await createService(input);
  if (key) svcBySticky.set(key, serviceId);
  records.set(serviceId, { vmId: serviceId, stickyKey: key, createdAt: new Date().toISOString() });
  return { serviceId, reused: false };
}

/** Wait until the service has a running instance we can exec into (bounded poll). */
async function waitRunning(serviceId: string, timeoutMs: number): Promise<void> {
  const c = await nf();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const st: any = await c.get.service({ parameters: { projectId: config.northflankProjectId, serviceId } });
      const status = st?.data?.status?.deployment?.status ?? st?.data?.status;
      if (String(status).toUpperCase().includes("RUNNING") || status === "COMPLETED") return;
    } catch { /* not ready yet */ }
    await new Promise((r) => setTimeout(r, 2000));
  }
}

async function dispatchRun(input: RunInput): Promise<RunReceipt> {
  if (!input.command) throw new Error("command is required");
  const { serviceId, reused } = await resolveService(input);
  if (!reused) await waitRunning(serviceId, 120_000);

  // Export the run env, then launch the runtime DETACHED so exec returns immediately (the runtime
  // drives the run against the shared DB + relay bus) — same shape as the Freestyle adapter.
  const envPrefix = Object.entries(input.env ?? {})
    .map(([k, v]) => `export ${k}=${shq(String(v))};`)
    .join(" ");
  const detached = `nohup sh -c ${shq(input.command)} >/tmp/ros-run-${input.runId}.log 2>&1 &`;
  const script = `${envPrefix} ${detached} echo launched`;

  const c = await nf();
  const execRes: any = await c.exec.execServiceCommand(
    { projectId: config.northflankProjectId, serviceId },
    { command: script, shell: "sh -c" },
  );
  ok(execRes, "exec run command");

  const rec = records.get(serviceId);
  if (rec) rec.lastRunId = input.runId;
  return { vmId: serviceId, runId: input.runId, reused };
}

async function runStatusFn(serviceId: string): Promise<RunStatus> {
  const alive = await serviceAlive(serviceId);
  return { vmId: serviceId, alive, record: records.get(serviceId) ?? null };
}

async function teardownVm(serviceId: string): Promise<void> {
  records.delete(serviceId);
  for (const [k, v] of svcBySticky) if (v === serviceId) svcBySticky.delete(k);
  const c = await nf();
  const res: any = await c.delete.service({ parameters: { projectId: config.northflankProjectId, serviceId } });
  ok(res, "delete service");
}

/** Shell single-quote escape (same as the Freestyle adapter). */
function shq(s: string): string {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

export const northflankProvider: ExecutionProvider = {
  name: "northflank",
  enabled: northflankEnabled,
  dispatchRun,
  runStatus: runStatusFn,
  teardownVm,
};
