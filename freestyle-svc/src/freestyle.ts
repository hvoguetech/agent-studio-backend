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
 * ROS_INSTALL_REPO_URL (a plain https URL) points at agent-studio-backend; the `ros` package lives in
 * apps/api. For a private clone, pass ROS_INSTALL_TOKEN separately — it is spliced into the URL VM-side
 * as an x-access-token so the token never lives in the repo URL itself.
 */
export const TOOLCHAIN_STEPS: string[] = [
  `export DEBIAN_FRONTEND=noninteractive`,
  `(apt-get update && apt-get install -y --no-install-recommends build-essential git curl ca-certificates gnupg jq ripgrep unzip postgresql-client sqlite3 python3-venv python3-pip nodejs npm) || true`,
  // Java — Amazon Corretto 21.0.2 (pinned). Install the exact Corretto build to /opt/jdk and put
  // java/javac on PATH via /usr/local/bin symlinks + JAVA_HOME in /etc/profile.d (login shells).
  // Arch-aware URL (x64/aarch64). Pinned version so the image's Java is deterministic.
  `CORRETTO_VER=21.0.2.13.1; ` +
    `ARCH="$(uname -m)"; case "$ARCH" in x86_64) CJ=x64;; aarch64|arm64) CJ=aarch64;; *) CJ=x64;; esac; ` +
    `(rm -rf /opt/jdk && mkdir -p /opt/jdk && ` +
    `echo "installing Amazon Corretto \${CORRETTO_VER} (\${CJ})…" && ` +
    `curl -fsSL --retry 3 --max-time 300 "https://corretto.aws/downloads/resources/\${CORRETTO_VER}/amazon-corretto-\${CORRETTO_VER}-linux-\${CJ}.tar.gz" -o /tmp/corretto.tgz && ` +
    `tar -xzf /tmp/corretto.tgz -C /opt/jdk --strip-components=1 && rm -f /tmp/corretto.tgz && ` +
    `ln -sf /opt/jdk/bin/java /usr/local/bin/java && ln -sf /opt/jdk/bin/javac /usr/local/bin/javac && ` +
    `printf 'export JAVA_HOME=/opt/jdk\\nexport PATH=/opt/jdk/bin:$PATH\\n' > /etc/profile.d/java.sh) || echo "JAVA INSTALL FAILED"`,
  // Clone the ros backend repo and install the `ros` package (prod extras) so `python -m ros.runtime`
  // resolves. ROS_INSTALL_REPO_URL is a plain https URL; ROS_INSTALL_TOKEN (when set) is spliced in
  // VM-side as an x-access-token for a private clone; ROS_INSTALL_REF optionally pins a branch/tag
  // (default: the repo default branch).
  `ROS_CLONE_URL="$ROS_INSTALL_REPO_URL"; if [ -n "$ROS_INSTALL_TOKEN" ]; then ROS_CLONE_URL="$(printf '%s' "$ROS_INSTALL_REPO_URL" | sed -E "s#^https://#https://x-access-token:\${ROS_INSTALL_TOKEN}@#")"; fi`,
  `(rm -rf /opt/ros-src && git clone --depth 1 \${ROS_INSTALL_REF:+--branch "$ROS_INSTALL_REF"} "$ROS_CLONE_URL" /opt/ros-src) || echo "CLONE FAILED"`,
  `(python3 -m pip install --break-system-packages -q -e "/opt/ros-src/apps/api[providers,vectors,knowledge,mcp,workers,postgres,storage,claude_code]" || python3 -m pip install -q -e "/opt/ros-src/apps/api[providers,vectors,knowledge,mcp,workers,postgres,storage,claude_code]") || echo "PIP INSTALL FAILED"`,
  // Claude Code CLI on PATH — the claude_code node's Claude Agent SDK spawns the `claude` CLI as a
  // subprocess from PATH. The SDK's pip wheel (claude-agent-sdk, pulled in by the claude_code extra
  // above) ships the matching linux binary at .../claude_agent_sdk/_bundled/claude — deterministic,
  // unlike npm's flaky platform-specific optionalDependency. Symlink that into /usr/local/bin (always
  // on PATH); fall back to an npm-installed binary if the bundled one is ever absent.
  `CLAUDE_BIN="$(find / -type f -path '*claude_agent_sdk*/_bundled/claude' 2>/dev/null | head -n1)"; ` +
    `if [ -z "$CLAUDE_BIN" ]; then (npm install -g @anthropic-ai/claude-code) >/dev/null 2>&1 || true; ` +
    `CLAUDE_BIN="$(command -v claude 2>/dev/null || find / -type f -path '*claude-code-linux-*/claude' 2>/dev/null | head -n1)"; fi; ` +
    `if [ -n "$CLAUDE_BIN" ]; then chmod +x "$CLAUDE_BIN" 2>/dev/null || true; ln -sf "$CLAUDE_BIN" /usr/local/bin/claude; else echo "CLAUDE CLI NOT FOUND"; fi`,
  // Bake the shared JSON schemas at the path config.py expects (/app/packages/schemas) so a manifest
  // run can validate node/tool config offline.
  `(mkdir -p /app && cp -r /opt/ros-src/packages /app/packages) || true`,
  // Bake a version-metadata manifest INTO the image at /opt/ros-image/versions.json so every VM
  // booted from this snapshot self-reports the exact runtimes it carries (python, node, java/JDK,
  // claude, ros). The bake reads this back to record image metadata; downstream can read it at runtime.
  `mkdir -p /opt/ros-image && ` +
    `PY="$(python3 --version 2>&1 | awk '{print $2}')"; ` +
    `NODE="$(node --version 2>/dev/null | sed 's/^v//')"; ` +
    `NPM="$(npm --version 2>/dev/null)"; ` +
    `JAVA="$(java -version 2>&1 | head -1 | sed -E 's/.*version \"([^\"]+)\".*/\\1/')"; ` +
    `JDK="amazoncorretto:21.0.2"; ` +
    `CLAUDE="$(claude --version 2>/dev/null | awk '{print $1}')"; ` +
    `ROS="$(python3 -c 'import ros; print(ros.__version__)' 2>/dev/null)"; ` +
    `BUILT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; ` +
    `printf '{\\n  "image": "ros-claude-backend",\\n  "built_at": "%s",\\n  "python": "%s",\\n  "node": "%s",\\n  "npm": "%s",\\n  "java": "%s",\\n  "jdk": "%s",\\n  "claude": "%s",\\n  "ros": "%s"\\n}\\n' ` +
    `"$BUILT" "$PY" "$NODE" "$NPM" "$JAVA" "$JDK" "$CLAUDE" "$ROS" > /opt/ros-image/versions.json; ` +
    `cat /opt/ros-image/versions.json`,
];
