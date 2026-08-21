import { Freestyle } from "freestyle";
import { config } from "../dist/config.js";

const vmId = process.env.VM_ID;
const runId = process.env.RUN_ID;
if (!vmId) { console.error("set VM_ID"); process.exit(2); }
const fs = new Freestyle({ apiKey: config.freestyleApiKey });
const { vm } = await fs.vms.get({ vmId });
// Head of the log (first errors / provider-key warnings) + count of node activity.
const cmd = `bash -lc 'echo "== run log HEAD =="; head -c 5000 /tmp/ros-run-${runId || "*"}.log 2>/dev/null || echo NO-LOG'`;
const res = await vm.exec({ command: cmd, timeoutMs: 60000 }).catch((e) => ({ stdout: "", stderr: String(e?.message ?? e) }));
console.log(`${res.stdout ?? ""}${res.stderr ?? ""}`);
