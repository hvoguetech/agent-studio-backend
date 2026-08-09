# Design — artifact storage (WS7)

**Goal:** durable, listable, downloadable storage for artifacts an agent/tool produces (reports,
images, CSVs, generated files) — offloaded out of run state / Postgres so it's robust and scalable.

**Why not run-state:** files in LangGraph state bloat checkpoints + per-run memory and aren't
downloadable/listable (see the robustness discussion). Bytes belong in an object store; only a
**reference** goes in state.

## 1. Layers / seam

```
ArtifactStore (service)         key scheme + content-addressing + resolver  (backend-agnostic)
   ├── BucketResolver           (tenant, project) -> (bucket, key-prefix)
   └── ObjectStore (backend)    raw put/get/delete/presign by (bucket, key)
         ├── local  (default)   filesystem/volume — zero infra, dev/single-node
         └── s3                 S3-compatible (Railway bucket / R2 / MinIO / AWS), lazy boto3
```
Selected by `ROS_ARTIFACT_STORE` (`local` default; `s3`), resolved once + cached — same pattern as
the execution/code-executor seams (the core imports boto3 only when `s3` is selected).

## 2. Bucket management (the core decision)

- **One SHARED bucket, key-prefix isolation** — NOT bucket-per-tenant (S3 caps ~1000 buckets/acct;
  per-tenant provisioning/IAM overhead). Shared bucket scales to millions of objects, zero
  per-tenant provisioning.
- **Key scheme (server-generated, never client-supplied):**
  ```
  {env}/{tenant_id}/{project_id}/{run_id}/{sha256}/{safe_filename}
  ```
  - `tenant_id` first → prefix-scoped access, per-tenant list/delete are trivial.
  - **content-addressed (sha256)** → idempotent writes (a re-run after a crash overwrites the same
    key — no dupes; the resume-safety invariant).
  - The app always derives `tenant_id/project_id` from the authenticated request and prepends them;
    a client never passes a path → no traversal, no cross-tenant reads. Gated by `artifact:read/write`.
- **Credentials:** the app holds ONE set of bucket creds (`ROS_S3_*`); users/agents never see them.
  Downloads = **presigned GET URLs** (short-lived, per-object, after an `artifact:read` check, served
  `Content-Disposition: attachment`). Uploads via the app or a presigned PUT to a server-generated key.
- **Enterprise escape hatch:** `BucketResolver` is the pluggable seam — a big/regulated tenant gets a
  **dedicated / BYO / region bucket** (physical isolation, per-tenant KMS, data residency) by
  swapping the resolver, without changing `ObjectStore` or callers. Start pooled; go dedicated per contract.

## 3. Resume / crash-safety invariants (must hold)

- **Reference-in-checkpoint, bytes-in-store** — state holds `ArtifactRef {bucket, key, sha256, size}`,
  never the bytes.
- **Write-ahead** — upload bytes *before* recording the ref; content-addressed keys make re-upload on
  retry a no-op overwrite. So a crash between upload and checkpoint → resume re-runs the node → same
  key, no garbage; a crash after checkpoint → ref restored + bytes durable → run continues.
- **Reference-aware GC** — never delete an artifact reachable from a live/resumable run (mirror the
  checkpoint-retention rule). S3 lifecycle only as a backstop for orphaned/failed uploads.

## 4. Side-effects / guardrails (from the risk review)

- New external dependency on the run hot-path → **timeouts + retries + graceful degradation** (fail
  the tool, not the run, on transient store errors).
- **Egress:** the SSRF guard (`egress_block_private`) must **allowlist** the bucket endpoint.
- New secret (`ROS_S3_*`) → include in the rotation set.
- Untrusted content → presigned, short-lived, `attachment` downloads; never inline-render.
- Cost/quota → per-tenant usage = sum object sizes by prefix; enforce a storage cap via the existing
  quota system.
- **Cascade delete:** project/tenant/run delete → `delete_prefix` under that prefix (extend the
  existing cascade). Bucket-per-tenant enterprise = drop the bucket.

## 5. Phasing

- **Phase 1 (this change): storage layer** — `ObjectStore` (local + s3), `BucketResolver`,
  `ArtifactStore` (key scheme, content-addressing, size cap), config + tests. No DB/API/deploy.
- **Phase 2: `Artifact` model + API** — DB table (tenant/project/run/key/sha/size/content_type),
  migration, upload/list/download(presign)/delete router gated by `artifact:read/write`.
- **Phase 3: producers + GC** — wire the deep-agent filesystem backend + a tool-artifact emit path to
  `ArtifactStore` (refs-in-state); ref-aware retention + cascade delete; per-tenant quota.
- **Phase 4 (enterprise): dedicated/BYO buckets** via a custom `BucketResolver`; region + KMS.

## 6. Config

`ROS_ARTIFACT_STORE` (local|s3) · `ROS_ARTIFACT_BUCKET` · `ROS_ARTIFACT_MAX_BYTES` (0=unlimited) ·
`ROS_S3_ENDPOINT_URL` · `ROS_S3_REGION` · `ROS_S3_ACCESS_KEY_ID` · `ROS_S3_SECRET_ACCESS_KEY`.
S3 backend needs the optional `[storage]` extra (boto3), lazy-imported.
