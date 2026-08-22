/**
 * Execution-provider interface shared by the Freestyle and Northflank adapters.
 *
 * The service is provider-agnostic: `config.provider` selects which adapter backs the HTTP endpoints
 * (`/run`, `/run/:id`, `DELETE /vm/:id`). ROS's ExecutionBackend keeps calling the SAME contract —
 * the receipt's `vmId` is just an opaque executor id (a Freestyle VM id or a Northflank service id).
 *
 * A run maps to "boot/reuse an executor for this stickyKey, launch `python -m ros.runtime drive` on
 * it (detached), return a receipt". Freestyle => a persistent microVM + vm.exec. Northflank => a warm
 * Service + execute-command (both reuse one executor per stickyKey / agent).
 */

export interface RunInput {
  runId: string;
  tenantId: string;
  projectId?: string | null;
  command: string;
  env?: Record<string, string>;
  stickyKey?: string;
  warm?: boolean;
}

export interface RunReceipt {
  vmId: string; // opaque executor id (Freestyle VM id | Northflank service id)
  runId: string;
  reused: boolean;
}

export interface ExecutorRecord {
  vmId: string;
  stickyKey?: string;
  lastRunId?: string;
  createdAt: string;
}

export interface RunStatus {
  vmId: string;
  alive: boolean;
  record: ExecutorRecord | null;
}

export interface ExecutionProvider {
  /** Provider name for /healthz + logs. */
  readonly name: string;
  /** True when the adapter has the config it needs to operate (creds, project, etc.). */
  enabled(): boolean;
  /** Boot/reuse an executor and launch the run's command on it (detached); return a receipt. */
  dispatchRun(input: RunInput): Promise<RunReceipt>;
  /** Best-effort status of an executor + the last run it launched. */
  runStatus(vmId: string): Promise<RunStatus>;
  /** Explicit teardown — the only thing that destroys an executor (lifecycle policy, GAPS G2). */
  teardownVm(vmId: string): Promise<void>;
}
