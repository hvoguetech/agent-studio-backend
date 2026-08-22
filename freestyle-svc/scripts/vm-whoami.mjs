// Identify which Freestyle account the configured key maps to, and fetch one VM by id.
//   VM_ID=<id> railway run node scripts/vm-whoami.mjs
import { Freestyle } from "freestyle";
const f = new Freestyle({ apiKey: process.env.FREESTYLE_API_KEY });
const who = await f.whoami().catch((e) => ({ __err: String(e?.message ?? e) }));
console.log("whoami:", JSON.stringify(who, null, 2));
const id = process.env.VM_ID;
if (id) {
  const got = await f.vms.get({ vmId: id }).catch((e) => ({ __err: String(e?.message ?? e) }));
  // vms.get returns { vm: <handle>, vmId, ... }; pull only serializable scalar fields.
  const src = got?.vm ?? got;
  const flat = {};
  for (const k of ["id", "vmId", "name", "state", "status", "createdAt", "deleted",
                   "snapshotId", "buildId", "accountId"]) {
    if (src && src[k] !== undefined) flat[k] = src[k];
  }
  if (src?.persistence?.type) flat.persistence = src.persistence.type;
  console.log(`vm ${id}:`, JSON.stringify(flat, null, 2));
}
