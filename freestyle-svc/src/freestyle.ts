/**
 * Freestyle VM lifecycle for the ros run-control service.
 *
 * ROS's freestyle ExecutionBackend POSTs `/run` with a `command` (always
 * `python -m ros.runtime drive --run-id <id> ...`) + `env` (ROS_MASTER_URL, ROS_RUNTIME_TOKEN, and
 * the run's injected DATABASE_URL/REDIS_URL). We:
 *   1. reuse a live VM for the request's stickyKey (one persistent VM per agent) if present, else
 *      create one — `persistence: "persistent"`, no idle timeout, so it runs until explicit teardown;
 *   2. `vm.exec` the command DETACHED (the runtime drives the run against the shared DB + relay bus,
 *      so we don't block on it — we only launch it and return a receipt);
 *   3. tear a VM down ONLY on `DELETE /vm/:id` (the chosen lifecycle policy, docs/GAPS.md G2).
 *
 * ⚠️ LIVE-VERIFY: the Freestyle SDK's exact create/exec shape (esp. detached exec + how a base VM
 * gets the `ros` runtime when no snapshot is baked) must be confirmed against the deployed platform.
 */
import { Freestyle } from "freestyle";
import { VmPython } from "@freestyle-sh/with-python";
import { config } from "./config.js";

let client: Freestyle | null = null;
function fs(): Freestyle {
  if (!client) client = new Freestyle({ apiKey: config.freestyleApiKey });
  return client;
}

export function freestyleEnabled(): boolean {
  return Boolean(config.freestyleApiKey);
}

/** In-memory stickyKey -> vmId map (one persistent VM per agent). Best-effort: on process restart we
 * re-verify liveness via vms.get before reuse, and ROS's own Run.executor row records the vm_id, so a
 * lost map entry just means the next /run creates a fresh VM rather than losing the old one silently. */
const vmsBySticky = new Map<string, string>();
/** vmId -> the last command/receipt, for GET /run/:id status. */
export interface VmRecord { vmId: string; stickyKey?: string; lastRunId?: string; createdAt: string; }
const records = new Map<string, VmRecord>();

export interface RunInput {
  runId: string;
  tenantId: string;
  projectId?: string | null;
  command: string;
  env?: Record<string, string>;
  stickyKey?: string;
  warm?: boolean;
}
export interface RunReceipt { vmId: string; runId: string; reused: boolean; }

/** Is a VM still alive on Freestyle? A gone/deleted VM throws → not alive. */
async function vmAlive(vmId: string): Promise<boolean> {
  try {
    await fs().vms.get({ vmId });
    return true;
  } catch {
    return false;
  }
}

/** Create a persistent VM (runs until explicit teardown). Uses a prebaked snapshot when configured
 * (fast, runtime already inside), else a base python VM. */
async function createVm(): Promise<{ vm: any; vmId: string }> {
  const persistence =
    config.persistence === "persistent" ? { type: "persistent" as const }
    : config.persistence === "ephemeral" ? { type: "ephemeral" as const }
    : { type: "sticky" as const, priority: config.stickyPriority };

  const createOpts: any = { persistence };
  // Only pass idleTimeoutSeconds when it's a positive value; 0/empty => no idle timeout at all.
  if (config.idleTimeoutSeconds > 0) createOpts.idleTimeoutSeconds = config.idleTimeoutSeconds;

  if (config.snapshotId) {
    createOpts.snapshotId = config.snapshotId;
  } else {
    // Fallback: declare a python runtime so `python` is on PATH. NOTE: this base VM does NOT have the
    // `ros` package installed — a baked snapshot (ROS_SNAPSHOT_ID) is the intended path (GAPS G2).
    createOpts.with = { python: new VmPython() };
  }

  const res: any = await fs().vms.create(createOpts);
  return { vm: res.vm, vmId: res.vmId };
}

/** Resolve the VM to run on: reuse the live sticky VM if present, else create a new persistent one. */
async function resolveVm(input: RunInput): Promise<{ vm: any; vmId: string; reused: boolean }> {
  const key = input.stickyKey;
  if (key) {
    const existing = vmsBySticky.get(key);
    if (existing && (await vmAlive(existing))) {
      const { vm } = await fs().vms.get({ vmId: existing });
      return { vm, vmId: existing, reused: true };
    }
    if (existing) vmsBySticky.delete(existing); // stale entry — VM gone
  }
  const { vm, vmId } = await createVm();
  if (key) vmsBySticky.set(key, vmId);
  records.set(vmId, { vmId, stickyKey: key, createdAt: new Date().toISOString() });
  return { vm, vmId, reused: false };
}

/** Launch the ros runtime command on the VM, DETACHED, and return a receipt. */
export async function dispatchRun(input: RunInput): Promise<RunReceipt> {
  if (!input.command) throw new Error("command is required");
  const { vm, vmId, reused } = await resolveVm(input);

  // Export the run's env, then launch the runtime detached (nohup + background) so the exec call
  // returns immediately — the runtime drives the run itself against the shared DB and relay bus.
  const envPrefix = Object.entries(input.env ?? {})
    .map(([k, v]) => `export ${k}=${shq(String(v))};`)
    .join(" ");
  const detached = `nohup sh -c ${shq(input.command)} >/tmp/ros-run-${input.runId}.log 2>&1 &`;
  const script = `${envPrefix} ${detached} echo launched`;

  await vmExec(vm, script, config.execTimeoutMs);

  const rec = records.get(vmId);
  if (rec) rec.lastRunId = input.runId;
  return { vmId, runId: input.runId, reused };
}

/** Best-effort status for a run/VM: whether the VM is alive + the last run it launched. */
export async function runStatus(vmId: string): Promise<{ vmId: string; alive: boolean; record: VmRecord | null }> {
  const alive = await vmAlive(vmId);
  return { vmId, alive, record: records.get(vmId) ?? null };
}

/** Explicit teardown — the ONLY thing that destroys a VM (the chosen lifecycle policy). */
export async function teardownVm(vmId: string): Promise<void> {
  records.delete(vmId);
  for (const [k, v] of vmsBySticky) if (v === vmId) vmsBySticky.delete(k);
  await fs().vms.delete({ vmId });
}

// --- helpers ------------------------------------------------------------------------------------

/** Run a short shell command on the VM. Shape confirmed against Atlas' service (execChecked). */
async function vmExec(vm: any, command: string, timeoutMs: number): Promise<string> {
  const res: any = await vm.exec(command, { timeout: timeoutMs });
  // Freestyle exec result shapes vary; be tolerant.
  return String(res?.stdout ?? res?.output ?? res ?? "");
}

/** Shell single-quote escape. */
function shq(s: string): string {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

/**
 * Toolchain baked into the `ros-claude-backend` snapshot image (build-image.ts). The whole point of
 * the image is that a VM booted from it can immediately run `python -m ros.runtime drive` AND host the
 * `claude_agent` tool/node — so it installs: system deps, Node + the `claude` CLI, the `ros` package
 * itself (with its prod extras) from the cloned repo, and the Claude Agent SDK. Steps are best-effort
 * (`|| true`) so one hiccup doesn't abort the whole bake; the verify step is what gates success.
 *
 * ROS_INSTALL_REPO_URL (an https URL, optionally with an embedded token for a private clone) points
 * at agent-studio-backend; the `ros` package lives in apps/api.
 */
export const TOOLCHAIN_STEPS: string[] = [
  `export DEBIAN_FRONTEND=noninteractive`,
  `(apt-get update && apt-get install -y --no-install-recommends build-essential git curl ca-certificates gnupg jq ripgrep unzip postgresql-client sqlite3 python3-venv python3-pip nodejs npm) || true`,
  // Claude Code CLI — makes the VM able to host the claude_agent tool/node.
  `(npm install -g @anthropic-ai/claude-code) || true`,
  // Clone the ros backend repo and install the `ros` package (prod extras) so `python -m ros.runtime`
  // resolves. ROS_INSTALL_REPO_URL may carry an x-access-token for a private clone; ROS_INSTALL_REF
  // optionally pins a branch/tag (default: the repo default branch).
  `(rm -rf /opt/ros-src && git clone --depth 1 \${ROS_INSTALL_REF:+--branch "$ROS_INSTALL_REF"} "$ROS_INSTALL_REPO_URL" /opt/ros-src) || echo "CLONE FAILED"`,
  `(python3 -m pip install --break-system-packages -q -e "/opt/ros-src/apps/api[providers,vectors,knowledge,mcp,workers,postgres,storage,claude_code]" || python3 -m pip install -q -e "/opt/ros-src/apps/api[providers,vectors,knowledge,mcp,workers,postgres,storage,claude_code]") || echo "PIP INSTALL FAILED"`,
  // Bake the shared JSON schemas at the path config.py expects (/app/packages/schemas) so a manifest
  // run can validate node/tool config offline.
  `(mkdir -p /app && cp -r /opt/ros-src/packages /app/packages) || true`,
];
