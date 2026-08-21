import { Freestyle } from "freestyle";
import { config } from "../dist/config.js";

const snapshotId = process.env.ROS_SNAPSHOT_ID;
if (!snapshotId) { console.error("set ROS_SNAPSHOT_ID"); process.exit(2); }
const fs = new Freestyle({ apiKey: config.freestyleApiKey });
console.log("probe VM from", snapshotId);
const boot = await fs.vms.create({ snapshotId, persistence: { type: "sticky", priority: 1 } });
try {
  const cmd =
    `bash -lc '` +
    `echo "python3: $(python3 --version 2>&1)"; ` +
    `echo "pip:     $(python3 -m pip --version 2>&1 | head -1)"; ` +
    `echo "node:    $(node --version 2>&1)"; ` +
    `echo "npm:     $(npm --version 2>&1)"; ` +
    `echo "npx:     $(npx --version 2>&1)"; ` +
    `echo "java:    $(java -version 2>&1 | head -1 || echo MISSING)"; ` +
    `echo "javac:   $(javac -version 2>&1 || echo MISSING)"; ` +
    `echo "go:      $(go version 2>&1 || echo MISSING)"; ` +
    `echo "ruby:    $(ruby --version 2>&1 || echo MISSING)"; ` +
    `echo "gcc:     $(gcc --version 2>&1 | head -1 || echo MISSING)"; ` +
    `echo "git:     $(git --version 2>&1)"; ` +
    `echo "curl:    $(curl --version 2>&1 | head -1)"; ` +
    `echo "rg:      $(rg --version 2>&1 | head -1 || echo MISSING)"; ` +
    `echo "psql:    $(psql --version 2>&1 || echo MISSING)"; ` +
    `echo "sqlite3: $(sqlite3 --version 2>&1 || echo MISSING)"; ` +
    `echo "claude:  $(claude --version 2>&1 || echo MISSING)"; ` +
    `echo "ros:     $(python3 -c "import ros; print(ros.__version__)" 2>&1)"` +
    `'`;
  const res = await boot.vm.exec({ command: cmd, timeoutMs: 90000 }).catch((e) => ({ stdout: "", stderr: String(e?.message ?? e) }));
  console.log(`${res.stdout ?? ""}${res.stderr ?? ""}`);
} finally {
  await boot.vm.delete().catch(() => {});
}
