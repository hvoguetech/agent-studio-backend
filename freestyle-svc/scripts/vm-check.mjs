import { Freestyle } from "freestyle";
import { config } from "../dist/config.js";

const snapshotId = process.env.ROS_SNAPSHOT_ID;
if (!snapshotId) { console.error("set ROS_SNAPSHOT_ID"); process.exit(2); }
const fs = new Freestyle({ apiKey: config.freestyleApiKey });
console.log("CHECK: booting VM from", snapshotId);
const boot = await fs.vms.create({ snapshotId, persistence: { type: "sticky", priority: 1 } });
try {
  const cmd =
    `bash -lc '` +
    `echo "== sandbox verb =="; python3 -m ros.runtime sandbox --help >/dev/null 2>&1 && echo sandbox-ok || echo NO-SANDBOX; ` +
    `echo "== drive verb =="; python3 -m ros.runtime drive --help >/dev/null 2>&1 && echo drive-ok || echo NO-DRIVE; ` +
    `echo "== sandbox module import =="; python3 -c "import ros.runtime.sandbox; import ros.execution.sandbox; print(\\"modules-ok\\")" 2>&1 | tail -1` +
    `'`;
  const res = await boot.vm.exec({ command: cmd, timeoutMs: 60000 }).catch((e) => ({ stdout: "", stderr: String(e?.message ?? e) }));
  console.log(`${res.stdout ?? ""}${res.stderr ?? ""}`);
} finally {
  await boot.vm.delete().catch(() => {});
}
