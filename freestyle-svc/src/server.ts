/**
 * ros run-control service — the HTTP API ROS's ExecutionBackend calls. Provider-agnostic: the
 * EXECUTION_PROVIDER env selects the adapter (freestyle | northflank); the contract is identical.
 *
 * Endpoints (all but /healthz require `Authorization: Bearer <FREESTYLE_SERVICE_SECRET>`):
 *   GET    /healthz        → liveness
 *   POST   /run            → boot/reuse an executor and launch `python -m ros.runtime drive` on it;
 *                            returns { vm_id, runId, reused }. Matches dispatch_run's body:
 *                            { runId, tenantId, projectId, command, env, stickyKey?, warm? }.
 *   GET    /run/:vmId       → status { vm_id, alive, record }.
 *   DELETE /vm/:vmId        → EXPLICIT teardown (the only thing that destroys an executor — GAPS G2).
 *
 * The executor runs until DELETE /vm/:vmId (persistent, no idle timeout by default).
 */
import { timingSafeEqual } from "node:crypto";
import Fastify from "fastify";
import { config } from "./config.js";
import { freestyleProvider } from "./freestyle.js";
import { northflankProvider } from "./northflank.js";
import type { ExecutionProvider, RunInput } from "./provider.js";

// Select the execution provider from config. Unknown value fails fast at boot.
const provider: ExecutionProvider =
  config.provider === "northflank" ? northflankProvider
  : config.provider === "freestyle" ? freestyleProvider
  : (() => { throw new Error(`unknown EXECUTION_PROVIDER: ${config.provider}`); })();

const app = Fastify({ logger: true, bodyLimit: 4 * 1024 * 1024 });

function secretMatches(token: string): boolean {
  const a = Buffer.from(token);
  const b = Buffer.from(config.serviceSecret);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Shared-secret gate on everything except the health check.
app.addHook("onRequest", async (req, reply) => {
  if (req.raw.url === "/healthz") return;
  const header = req.headers["authorization"];
  const token = typeof header === "string" && header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!token || !secretMatches(token)) {
    return reply.code(401).send({ error: "unauthorized" });
  }
});

app.get("/healthz", async () => ({
  ok: true, provider: provider.name, enabled: provider.enabled(), persistence: config.persistence,
}));

app.post<{ Body: RunInput }>("/run", async (req, reply) => {
  if (!provider.enabled()) return reply.code(503).send({ error: `${provider.name} not configured` });
  const body = req.body;
  if (!body?.runId || !body?.command) {
    return reply.code(400).send({ error: "runId and command are required" });
  }
  try {
    const receipt = await provider.dispatchRun(body);
    // ROS reads `vm_id` off the receipt (freestyle.py:_record_executor); include camelCase too.
    return reply.code(202).send({ vm_id: receipt.vmId, vmId: receipt.vmId, runId: receipt.runId, reused: receipt.reused });
  } catch (err) {
    req.log.error({ err }, "dispatchRun failed");
    return reply.code(500).send({ error: (err as Error).message });
  }
});

app.get<{ Params: { vmId: string } }>("/run/:vmId", async (req, reply) => {
  try {
    const status = await provider.runStatus(req.params.vmId);
    return reply.send({ vm_id: status.vmId, alive: status.alive, record: status.record });
  } catch (err) {
    return reply.code(500).send({ error: (err as Error).message });
  }
});

app.delete<{ Params: { vmId: string } }>("/vm/:vmId", async (req, reply) => {
  try {
    await provider.teardownVm(req.params.vmId);
    return reply.send({ ok: true, vm_id: req.params.vmId });
  } catch (err) {
    req.log.error({ err }, "teardownVm failed");
    return reply.code(500).send({ error: (err as Error).message });
  }
});

const start = async () => {
  try {
    await app.listen({ port: config.port, host: "0.0.0.0" });
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
};
start();
