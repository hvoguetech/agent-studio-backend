/**
 * Build the prebaked `ros-claude-backend` Freestyle snapshot image (solves GAPS G2's blocker).
 *
 * Creates a builder VM, installs the ros runtime toolchain (Python + the `ros` package + the `claude`
 * CLI + the Claude Agent SDK), snapshots it as `ros-claude-backend`, then VERIFIES a fresh VM booted
 * from that snapshot can run `python -m ros.runtime` and `claude`. Prints the snapshotId to set as
 * ROS_SNAPSHOT_ID on the service.
 *
 * Run with the service env injected + the repo to install:
 *   npm run build
 *   ROS_INSTALL_REPO_URL="https://x-access-token:<gh_token>@github.com/hvoguetech/agent-studio-backend.git" \
 *   ROS_INSTALL_REF="main" \
 *   FREESTYLE_API_KEY=... FREESTYLE_SERVICE_SECRET=... node dist/build-image.js
 */
import { Freestyle } from "freestyle";
import { VmPython } from "@freestyle-sh/with-python";
import { config } from "./config.js";
import { TOOLCHAIN_STEPS } from "./freestyle.js";

const IMAGE_NAME = "ros-claude-backend";
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function main() {
  const repoUrl = process.env.ROS_INSTALL_REPO_URL;
  if (!repoUrl) {
    console.error("BUILD-IMAGE: ROS_INSTALL_REPO_URL is required (https clone URL for agent-studio-backend, may embed an x-access-token).");
    process.exit(2);
  }
  const fs = new Freestyle({ apiKey: config.freestyleApiKey });

  console.log(`BUILD-IMAGE: creating builder VM (python) for ${IMAGE_NAME}…`);
  const created: any = await fs.vms.create({
    with: { python: new VmPython() },
    persistence: { type: "sticky", priority: 1 },
    idleTimeoutSeconds: 30 * 60,
  } as any);
  const vm: any = created.vm;
  const vmId: string = created.vmId;
  console.log("BUILD-IMAGE: builder vm", vmId);

  let snapshotId = "";
  let verifyOut = "";
  try {
    // Install DETACHED (a multi-minute blocking exec drops Freestyle's exec HTTP connection): write
    // the build script with the repo env, launch it in the background, then poll a ready marker.
    const env =
      `export ROS_INSTALL_REPO_URL=${shq(repoUrl)}\n` +
      (process.env.ROS_INSTALL_REF ? `export ROS_INSTALL_REF=${shq(process.env.ROS_INSTALL_REF)}\n` : "");
    const script =
      `#!/usr/bin/env bash\n` +
      env +
      `${TOOLCHAIN_STEPS.join("\n")}\n` +
      `touch /tmp/image.ready\n`;
    await vm.fs.writeTextFile(`/tmp/build.sh`, script);
    await vm.exec({ command: `bash -lc 'setsid nohup bash /tmp/build.sh >/tmp/build.out 2>&1 </dev/null & echo launched'`, timeoutMs: 20000 });
    console.log("BUILD-IMAGE: toolchain install launched; polling (up to 20m)…");

    let ready = false;
    for (let i = 0; i < 240; i++) { // 240 × 5s = 20 min
      await sleep(5000);
      const res: any = await vm
        .exec({ command: `bash -lc 'test -f /tmp/image.ready && echo READY || tail -1 /tmp/build.out 2>/dev/null'`, timeoutMs: 15000 })
        .catch(() => ({ stdout: "" }));
      const out = `${res.stdout ?? ""}`.trim();
      if (out.includes("READY")) { ready = true; break; }
      if (i % 6 === 0) console.log(`  [${i * 5}s] ${out.slice(-160)}`);
    }
    if (!ready) throw new Error("toolchain install did not finish within 20m");

    const versions: any = await vm.exec({
      command: `bash -lc 'python3 --version; node --version 2>/dev/null || echo no-node; claude --version 2>/dev/null || echo no-claude; python3 -c "import ros; print(\\"ros\\", ros.__version__)" 2>/dev/null || echo no-ros; python3 -c "import claude_agent_sdk" 2>/dev/null && echo agent-sdk-ok || echo no-agent-sdk; python3 -m ros.runtime --help >/dev/null 2>&1 && echo runtime-ok || echo no-runtime'`,
      timeoutMs: 30000,
    });
    console.log("BUILD-IMAGE: builder tool versions →\n" + `${versions.stdout ?? ""}`);

    console.log(`BUILD-IMAGE: snapshotting as ${IMAGE_NAME}…`);
    const snap: any = await vm.snapshot({ name: IMAGE_NAME });
    snapshotId = snap.snapshotId;
    let st: any = await fs.vms.snapshots.get({ snapshotId });
    for (let i = 0; i < 120 && st.state === "building"; i++) {
      await sleep(5000);
      st = await fs.vms.snapshots.get({ snapshotId });
      if (i % 4 === 0) console.log(`  snapshot state=${st.state}`);
    }
    console.log(`BUILD-IMAGE: snapshot ${snapshotId} → ${st.state}`);
    if (st.state !== "ready") throw new Error(`snapshot not ready: ${st.state}`);

    // VERIFY: boot a fresh VM FROM the snapshot (no `with:`) and confirm the ros runtime + claude CLI.
    console.log("BUILD-IMAGE: verifying a fresh VM booted from the snapshot…");
    const boot: any = await fs.vms.create({ snapshotId, persistence: { type: "sticky", priority: 1 } } as any);
    const v: any = await boot.vm
      .exec({ command: `bash -lc 'python3 --version && (python3 -c "import ros; print(\\"ros\\", ros.__version__)" || echo no-ros) && (python3 -m ros.runtime --help >/dev/null 2>&1 && echo runtime-ok || echo no-runtime) && (claude --version 2>/dev/null || echo no-claude) && (python3 -c "import claude_agent_sdk" 2>/dev/null && echo agent-sdk-ok || echo no-agent-sdk)'`, timeoutMs: 30000 })
      .catch((e: any) => ({ stdout: "", stderr: String(e?.message ?? e) }));
    verifyOut = `${v.stdout ?? ""}${v.stderr ?? ""}`.trim();
    console.log("BUILD-IMAGE: from-snapshot check →\n" + verifyOut);
    await boot.vm.delete().catch(() => {});
  } finally {
    await vm.delete().catch(() => {});
  }

  const ok = snapshotId && verifyOut.includes("runtime-ok") && verifyOut.includes("ros ");
  console.log(`\nBUILD-IMAGE: ${ok ? "SUCCESS" : "REVIEW OUTPUT"} — set on ros-freestyle-svc:\n  ROS_SNAPSHOT_ID=${snapshotId}`);
  process.exit(snapshotId ? 0 : 1);
}

/** Shell single-quote escape. */
function shq(s: string): string {
  return `'${String(s).replace(/'/g, `'\\''`)}'`;
}

main().catch((e) => { console.error("BUILD-IMAGE: ERROR →", e); process.exit(2); });
