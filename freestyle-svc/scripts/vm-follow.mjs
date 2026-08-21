// Follow a run's log on a Freestyle VM (a tail -f substitute).
//
// vm.exec is one-shot (no streaming), so this polls the log file on a loop and prints only the
// bytes that are new since the last poll — approximating `tail -f`. Claude Code runs inside the
// detached ros runtime, so its output lands in the same /tmp/ros-run-<runId>.log file.
//
// Usage (from freestyle-svc/, after `npm run build`):
//   VM_ID=<vmId> npm run build && node scripts/vm-follow.mjs
//   VM_ID=<vmId> RUN_ID=<runId> node scripts/vm-follow.mjs   # pin a specific run
//   VM_ID=<vmId> CLAUDE_ONLY=1 node scripts/vm-follow.mjs     # only lines mentioning claude_code
//   VM_ID=<vmId> INTERVAL_MS=1500 node scripts/vm-follow.mjs  # poll cadence (default 2000ms)
//
// Ctrl-C to stop.
import { Freestyle } from "freestyle";
import { config } from "../dist/config.js";

const vmId = process.env.VM_ID;
const runId = process.env.RUN_ID || "*";
const intervalMs = Number(process.env.INTERVAL_MS || "2000");
const claudeOnly = process.env.CLAUDE_ONLY === "1";
if (!vmId) { console.error("set VM_ID"); process.exit(2); }

const fs = new Freestyle({ apiKey: config.freestyleApiKey });
const { vm } = await fs.vms.get({ vmId });

const logGlob = `/tmp/ros-run-${runId}.log`;
console.error(`[vm-follow] vm=${vmId} log=${logGlob} interval=${intervalMs}ms claudeOnly=${claudeOnly}`);
console.error(`[vm-follow] polling for new output — Ctrl-C to stop`);

// Track how many bytes we've already printed so each poll emits only the tail delta.
let seen = 0;
let missingWarned = false;

async function poll() {
  // Resolve the newest matching log file (RUN_ID may be a glob), then print bytes after `seen`.
  // `wc -c` gives current size; `tail -c +N` streams from a byte offset (1-based).
  const cmd =
    `bash -lc '` +
    `f="$(ls -t ${logGlob} 2>/dev/null | head -n1)"; ` +
    `if [ -z "$f" ]; then echo "__NOLOG__"; exit 0; fi; ` +
    `echo "__FILE__ $f"; ` +
    `sz="$(wc -c < "$f" 2>/dev/null || echo 0)"; echo "__SIZE__ $sz"; ` +
    `tail -c +${seen + 1} "$f" 2>/dev/null` +
    `'`;
  let out = "";
  try {
    const res = await vm.exec({ command: cmd, timeoutMs: 30000 });
    out = String(res?.stdout ?? res?.output ?? "") + String(res?.stderr ?? "");
  } catch (e) {
    console.error(`[vm-follow] exec error: ${String(e?.message ?? e)}`);
    return;
  }

  const lines = out.split("\n");
  let body = [];
  let size = seen;
  for (const line of lines) {
    if (line === "__NOLOG__") {
      if (!missingWarned) { console.error(`[vm-follow] no log yet at ${logGlob} — waiting…`); missingWarned = true; }
      return;
    }
    if (line.startsWith("__FILE__ ")) { missingWarned = false; continue; }
    if (line.startsWith("__SIZE__ ")) { size = Number(line.slice(9).trim()) || seen; continue; }
    body.push(line);
  }

  // If the file shrank/rotated (size < seen), restart from the top.
  if (size < seen) { seen = 0; return; }
  seen = size;

  let text = body.join("\n");
  if (claudeOnly) {
    text = text.split("\n").filter((l) => /claude[_ ]?code|ClaudeCode|claude_agent/i.test(l)).join("\n");
  }
  if (text.trim()) process.stdout.write(text.endsWith("\n") ? text : text + "\n");
}

// Poll loop. Serialize polls so a slow exec doesn't overlap.
while (true) {
  await poll();
  await new Promise((r) => setTimeout(r, intervalMs));
}
