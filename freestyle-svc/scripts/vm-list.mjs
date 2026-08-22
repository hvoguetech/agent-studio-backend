// List all Freestyle VMs on the account. Run with the service's env injected:
//   railway run node scripts/vm-list.mjs      (from freestyle-svc/, linked to that service)
// Reads FREESTYLE_API_KEY from the environment; never hard-codes it.
import { Freestyle } from "freestyle";

const key = process.env.FREESTYLE_API_KEY;
if (!key) { console.error("FREESTYLE_API_KEY not set"); process.exit(2); }
const f = new Freestyle({ apiKey: key });

// The SDK's list shape can vary; be tolerant about the returned container + item fields.
const res = await f.vms.list({}).catch((e) => ({ __error: String(e?.message ?? e) }));
if (res && res.__error) { console.error("list failed:", res.__error); process.exit(1); }

const items = Array.isArray(res) ? res
  : (res?.vms ?? res?.data ?? res?.items ?? []);
console.log(`total VMs: ${items.length}`);
for (const v of items) {
  const id = v.vmId ?? v.id ?? v.vm_id ?? "?";
  const status = v.status ?? v.state ?? "";
  const persistence = v?.persistence?.type ?? v.persistence ?? "";
  const created = v.createdAt ?? v.created_at ?? "";
  console.log(`- ${id}  ${status}  ${persistence}  ${created}`);
}
// Dump the raw first item so we can see the real field names if the guesses above miss.
if (items[0]) console.log("\nraw[0]:", JSON.stringify(items[0], null, 2));
