# Migration — Chroma → pgvector (multi-replica prerequisite, WS6.2)

**Why:** `ROS_VECTOR_BACKEND=chroma` (default) stores vectors in an on-disk Chroma index on the
api's `/app/.data` volume — **single-writer**. The moment you run **2+ api/worker replicas** that
touch knowledge or memory, they'd fight over that index and diverge. `pgvector` stores vectors in
Postgres (reusing `ROS_DATABASE_URL`), so every replica shares them. Do this **before** scaling
past one replica. At a single replica, Chroma is fine — this is prep, not urgent.

**Scope:** affects **knowledge** vectors (RAG sources / Q&A) and **long-term memory** vectors.
Chroma data does **not** auto-migrate — vectors must be **re-embedded** into Postgres.

---

## Step 1 — enable the extension (YOU run this; it modifies the DB)

pgvector must exist in the Postgres instance before the app writes vectors. Railway Postgres
supports it. Connect to the DB and run:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Easiest: `railway connect Postgres` (opens psql), then paste the statement. Verify:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

> If `CREATE EXTENSION` errors with "extension \"vector\" is not available", the Postgres image
> lacks pgvector — switch the Railway Postgres to the pgvector-enabled image first, then retry.

## Step 2 — point the app at pgvector (both services)

```bash
railway variables --service api    --set 'ROS_VECTOR_BACKEND=pgvector'
railway variables --service worker --set 'ROS_VECTOR_BACKEND=pgvector'
```

The vector tables (`ros_mem_<dim>` for memory, plus the knowledge collection tables) are created
on first write — no separate schema migration needed, only the extension from Step 1.

## Step 3 — re-embed existing knowledge (data doesn't carry over from Chroma)

Vectors already in Chroma won't appear in pgvector until re-ingested. For each project's knowledge
sources, trigger a re-ingest so embeddings are written to Postgres:

- **Console:** Knowledge → each source → **Re-ingest** (and **Rechunk** if offered).
- **API:** `POST /v1/projects/{project_id}/knowledge/sources/{source_id}/reingest`
  (or `POST .../knowledge/sources/rechunk` for a batch).

Long-term **memory** vectors re-accumulate as agents run; there's no bulk re-embed for past
memories (acceptable — they're recall aids, not source-of-truth). If needed, treat memory as
starting fresh under pgvector.

## Step 4 — (optional) ANN index for speed at scale

Exact search is fine to start. For large corpora add an HNSW index on the embedding column
(A/C7 #9) once you see query latency — e.g.:

```sql
-- adjust table/column to the actual vector table; run after data is populated
CREATE INDEX IF NOT EXISTS ix_<table>_embedding_hnsw
  ON <table> USING hnsw (embedding vector_cosine_ops);
```

## Step 5 — verify, then scale

- Confirm a knowledge search returns results and `railway logs --service api` shows no vector
  errors.
- Only now scale api/worker replicas past 1 (the other multi-replica prereqs — `ROS_SECRET_KEY`,
  Postgres checkpointer, Redis, leader-election — are already in place).

## Rollback

Set `ROS_VECTOR_BACKEND=chroma` on both services and redeploy — the Chroma index is still on the
volume, so you're back to the prior state (single-replica only).
