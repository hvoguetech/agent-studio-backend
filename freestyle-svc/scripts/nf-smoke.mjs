// End-to-end Northflank run-Service smoke test: create a throwaway deployment Service from the
// configured image source (internal build ref, else external image), wait for it to run, exec a
// toolchain probe, then ALWAYS delete it. Validates the northflank adapter's create/exec/teardown
// path against a real project.
//
// Usage (from freestyle-svc/, with .env holding NORTHFLANK_* creds):
//   node scripts/nf-smoke.mjs
//   KEEP=1 node scripts/nf-smoke.mjs     # leave the service running (skip teardown) for inspection
//
// Reads NORTHFLANK_API_TOKEN / NORTHFLANK_PROJECT_ID / NORTHFLANK_BUILD_SERVICE_ID(+BRANCH/SHA) or
// NORTHFLANK_RUN_IMAGE from the environment (loads .env if present). Never prints the token.
import fs from "node:fs";

// Minimal .env loader (no dep); does not overwrite already-set vars, never logs values.
if (fs.existsSync(".env")) {
  for (const line of fs.readFileSync(".env", "utf8").split(/\r?\n/)) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/i);
    if (m && !(m[1] in process.env)) process.env[m[1]] = m[2].replace(/^['"]|['"]$/g, "");
  }
}

const token = process.env.NORTHFLANK_API_TOKEN;
const pid = process.env.NORTHFLANK_PROJECT_ID;
if (!token || !pid) { console.error("set NORTHFLANK_API_TOKEN and NORTHFLANK_PROJECT_ID"); process.exit(2); }

function imageSource() {
  if (process.env.NORTHFLANK_BUILD_SERVICE_ID) {
    return { internal: {
      id: process.env.NORTHFLANK_BUILD_SERVICE_ID,
      branch: process.env.NORTHFLANK_BUILD_BRANCH || "main",
      buildSHA: process.env.NORTHFLANK_BUILD_SHA || "latest",
    } };
  }
  if (process.env.NORTHFLANK_RUN_IMAGE) return { external: { imagePath: process.env.NORTHFLANK_RUN_IMAGE } };
  console.error("set NORTHFLANK_BUILD_SERVICE_ID (internal) or NORTHFLANK_RUN_IMAGE (external)");
  process.exit(2);
}

const { ApiClient, ApiClientInMemoryContextProvider } = await import("@northflank/js-client");
const ctx = new ApiClientInMemoryContextProvider();
await ctx.addContext({ name: "ros", token });
const c = new ApiClient(ctx);

function ok(res, what) {
  if (res?.error) throw new Error(`${what}: ${res.error.status ?? ""} ${res.error.message ?? "failed"}`.trim());
  return res?.data;
}

const name = "ros-smoke-" + Date.now().toString(36);
let sid;
try {
  console.log("creating run Service", name, "from", JSON.stringify(imageSource()));
  const created = ok(await c.create.service.deployment({
    parameters: { projectId: pid },
    data: {
      name,
      billing: { deploymentPlan: process.env.NORTHFLANK_PLAN || "nf-compute-20" },
      deployment: { instances: 1, ...imageSource(), docker: { configType: "customCommand", customCommand: "sh -c 'sleep infinity'" } },
      ...(process.env.NORTHFLANK_REGION ? { region: process.env.NORTHFLANK_REGION } : {}),
    },
  }), "create service");
  sid = created?.id ?? created?.service?.id;
  if (!sid) throw new Error("no service id in create response");
  console.log("CREATE ok. serviceId =", sid);

  // Wait for a running instance.
  let running = false;
  for (let i = 0; i < 45; i++) {
    const st = ok(await c.get.service({ parameters: { projectId: pid, serviceId: sid } }), "get service");
    const status = st?.status?.deployment?.status;
    if (i % 4 === 0) console.log("  status:", status);
    if (String(status).toUpperCase().includes("RUNNING") || status === "COMPLETED") { running = true; break; }
    await new Promise((r) => setTimeout(r, 4000));
  }
  if (!running) throw new Error("service did not reach running within timeout");

  // Toolchain probe — proves it's the ros-claude image.
  const probe = "python3 --version; java -version 2>&1 | head -1; " +
    "python3 -c 'import ros; print(\"ros\", ros.__version__)'; " +
    "claude --version 2>&1 | head -1; " +
    "echo \"WS=$ROS_CLAUDE_CODE_WORKSPACE SANDBOX=$IS_SANDBOX\"; git --version; rg --version | head -1";
  const ex = await c.exec.execServiceCommand({ projectId: pid, serviceId: sid }, { command: probe, shell: "sh -c" });
  const d = ex?.data ?? ex;
  console.log("EXEC exit:", d?.commandResult?.exitCode);
  console.log("---- probe output ----\n" + (d?.stdOut || "").trim());
  if ((d?.stdErr || "").trim()) console.log("---- stderr ----\n" + (d.stdErr).trim().slice(0, 400));
  console.log("SMOKE OK");
} catch (e) {
  console.error("SMOKE FAILED:", String(e?.message ?? e));
  process.exitCode = 1;
} finally {
  if (sid && process.env.KEEP !== "1") {
    try { ok(await c.delete.service({ parameters: { projectId: pid, serviceId: sid } }), "delete service"); console.log("TEARDOWN ok:", sid); }
    catch (e) { console.error("TEARDOWN FAILED (delete it manually):", sid, String(e?.message ?? e)); }
  } else if (sid) {
    console.log("KEEP=1 — left service running:", sid, "(delete it when done)");
  }
}
