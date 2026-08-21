import { Freestyle } from "freestyle";
import { config } from "../dist/config.js";

const vmId = process.env.VM_ID;
const runId = process.env.RUN_ID;
if (!vmId) { console.error("set VM_ID"); process.exit(2); }
const fs = new Freestyle({ apiKey: config.freestyleApiKey });
const { vm } = await fs.vms.get({ vmId });
const cmd =
  `bash -lc '` +
  `echo "== run log tail =="; tail -c 4000 /tmp/ros-run-${runId || "*"}.log 2>/dev/null || echo NO-LOG; ` +
  `echo "== drive --help =="; python3 -m ros.runtime drive --help 2>&1 | head -20 || echo DRIVE-HELP-FAILED; ` +
  `echo "== can import ros =="; python3 -c "import ros; print(ros.__version__)" 2>&1; ` +
  `echo "== env sanity =="; echo "MASTER=$ROS_MASTER_URL"; echo "DB set: $([ -n \\"$DATABASE_URL\\" ] && echo yes || echo no)"; echo "REDIS set: $([ -n \\"$ROS_REDIS_URL\\" ] && echo yes || echo no)"` +
  `'`;
const res = await vm.exec({ command: cmd, timeoutMs: 60000 }).catch((e) => ({ stdout: "", stderr: String(e?.message ?? e) }));
console.log(`${res.stdout ?? ""}${res.stderr ?? ""}`);
