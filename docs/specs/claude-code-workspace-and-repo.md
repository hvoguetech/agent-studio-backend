# Spec: Claude Code node — stable per-node workspace + optional GitHub repo checkout

Status: **DRAFT for review** · Owner: platform · Scope: `claude_code` node + minimal engine plumbing

## Problem

The `claude_code` node runs the Claude Agent SDK in a working directory (`cwd`) where the agent
reads, edits, runs shell, and **creates files**. Today that directory is broken in two ways
(`apps/api/ros/nodes/claude_code.py`, `_resolve_cwd` + `claude_code_factory`):

1. **No per-workflow / per-node directory.** With no explicit `config.workspace`, the node falls back
   to `tempfile.mkdtemp(prefix="ros-claude-code-")`. The documented `ROS_CLAUDE_CODE_WORKSPACE` env
   ("set by the runtime on a VM") has **no writer anywhere** in the repo — so on the VM it also lands
   in an ad-hoc temp dir.
2. **`cwd` is resolved once in the factory, not per run.** `cwd = _resolve_cwd(config)` executes at
   **compile time**, outside the `_node(state)` closure, so the same directory is reused for every
   invocation of that compiled graph — keyed to neither `workflow_id`, `node_id`, nor `run_id`.

Consequences: files the agent generates are not placed in a predictable, per-node location; two
different claude nodes in the same workflow can collide on a shared temp dir; and there's no way for
a user to have the agent operate on an existing codebase.

## Goals

- A **stable, per-node** workspace: `<base>/<workflow_id>/<node_id>`, reused across runs so a
  stateful repo agent keeps its working tree between turns (chosen default).
- Node ids are only unique **within** a workflow, so the path MUST include `workflow_id` — two
  workflows can each have a node `cc`.
- An **optional GitHub repo** the node checks out into that workspace before running, configured by
  the user in the UI. Private repos authenticate via a **stored secret reference**, never a plaintext
  token in the workflow JSON.
- Close the `ROS_CLAUDE_CODE_WORKSPACE` gap: the runtime sets a stable base on the VM.

## Non-goals

- Artifact collection / upload of generated files (separate effort; see
  `docs/specs/artifacts-and-code-node.md`).
- Per-run isolated workspaces (`.../<run_id>/`). Deferred — the chosen default is persistent-per-node.
  Left as a future `workspace_scope` toggle (see Open questions).
- Non-GitHub sources (GitLab/Bitbucket/arbitrary git URL). The field accepts any https git URL, but
  auth UX is designed around a GitHub token secret.

## Decisions (from review)

1. **Default persistence:** persistent per node — `<base>/<workflow_id>/<node_id>`, reused across runs.
2. **Repo auth:** reference a stored secret by ref (`repo_secret_ref` → `secret://proj/<name>`). The
   token is resolved at run time and spliced in VM-side as an `x-access-token`; it never lives in the
   workflow definition, the trace, or the prompt.
3. **Checkout semantics:** **clone once**. If the workspace already contains the repo (a `.git` dir),
   **leave the working tree as-is** — the agent keeps whatever it changed on prior runs. No fetch,
   no reset, no re-clone.
4. **Factory plumbing:** back-compatible — the compiler passes `node_id` only to factories that opt
   in (accept it); existing 2-arg factories are unchanged.
5. **`repo_ref` in v1:** **branch or tag only** (shallow `--depth 1 --branch <ref>`). Arbitrary commit
   SHA is out of v1 (would need a deeper/non-shallow fetch).
6. **Workspace GC:** out of scope here; tie cleanup of `<base>/<workflow_id>/<node_id>` to the VM
   lifecycle GC (GAPS G2).

## Design

### 1. Directory resolution (move into `_node`, key by workflow + node)

Base precedence stays, but resolution moves from the factory into the per-invocation closure so it
can key on the node identity:

```
base = config.workspace            # explicit absolute path wins (unchanged escape hatch)
     | ROS_CLAUDE_CODE_WORKSPACE   # per-VM root the runtime now sets (e.g. /workspace)
     | <temp root>                 # tempfile.gettempdir()/ros-claude-code  (dev / no VM)

cwd  = base                        # when config.workspace is an explicit absolute path, use it verbatim
     | base / workflow_id / node_id   # otherwise the stable per-node dir
```

- If `config.workspace` is set explicitly, honor it verbatim (back-compat; power users pin a path).
- Otherwise compose `<base>/<workflow_id>/<node_id>`, `os.makedirs(..., exist_ok=True)`, absolute.
- `node_id` and `workflow_id` must reach the node (see plumbing). If either is unavailable
  (e.g. an ad-hoc compile with no workflow id), fall back to the current temp-dir behavior so nothing
  regresses.

### 2. Engine plumbing (small, additive)

- **Pass the node id to factories (back-compatible).** `compiler.py:152` calls
  `spec.factory(config, ctx)`. The compiler will pass `node_id` **only to factories that accept it**:
  inspect the factory signature (arity / `node_id` kwarg) and call `factory(config, ctx, node_id=...)`
  when supported, else `factory(config, ctx)` as today. Only `claude_code_factory` opts in; every
  existing 2-arg factory is untouched.
- **Add `workflow_id` to `CompileContext`** (`engine/context.py`), populated by the compiler /
  runtime assembler from `wf.id`. Optional (`None`) to keep the dataclass unit-testable.

### 3. Repo checkout (inside `_node`, before `query()`)

New config fields: `repo_url`, `repo_ref` (branch/tag/sha), `repo_secret_ref`.

Order of operations in `_node`, after resolving `cwd`:

```
if repo_url:
    if (cwd / ".git") exists:      # clone-once: keep the agent's working tree
        pass
    else:
        token = resolve_secret(repo_secret_ref) if repo_secret_ref else None
        url   = splice_x_access_token(repo_url, token)   # https://x-access-token:<tok>@github.com/...
        git clone --depth 1 [--branch <repo_ref>] <url> <cwd>   # into the (empty) workspace
```

- The clone runs **inside the VM** (git + network live there), i.e. in `_node`, never on the control
  plane. Uses the same `x-access-token` splice the image bake already uses
  (`freestyle-svc/src/freestyle.ts` TOOLCHAIN_STEPS) so the token never appears in the URL at rest.
- **Secret resolution** uses the existing `SecretStore` choke point (`ros/secrets/store.py`,
  `secret://proj/<name>`, tenant/project-scoped, audited). On a trusted VM the store reads master DB
  directly; on the isolating sandbox it resolves from the manifest's `InMemorySecretStore`
  (`runtime/secret_source.py`) — so master must include `repo_secret_ref` in the resolved-secret set
  it embeds in the manifest. Resolution is done via `ctx` (which already carries `tenant_id` /
  `project_id` and the resolver), not a fresh DB hit from the node.
- The token is used only to build the clone URL for the subprocess; it is **not** written into
  `os.environ`, the AIMessage, `response_metadata`, or any log. `git clone` output is captured like
  other subprocess output; on failure it surfaces through the existing `ClaudeCodeError` path (the
  error-capture fix) — but we must scrub any `x-access-token:...@` substring from that text.

### 4. Runtime sets the base (close the gap)

The runtime entrypoint on the VM exports `ROS_CLAUDE_CODE_WORKSPACE=/workspace` (a writable root on
the VM) alongside the other run env it applies (`ros.runtime.env.apply_runtime_env`). This gives a
stable base so the node's `<base>/<workflow_id>/<node_id>` is deterministic per VM. On master/dev
(no VM) the temp-root fallback keeps working.

## Schema changes (`packages/schemas/ros/nodes/claude_code.json`)

Add (all optional, `additionalProperties:false` stays):

| field | type | UI | notes |
|---|---|---|---|
| `repo_url` | string | text | https git URL; blank = no checkout. |
| `repo_ref` | string | text | **branch or tag** (v1); blank = default branch. Arbitrary commit SHA not supported in v1 (shallow clone). |
| `repo_secret_ref` | string | **secret picker** | `secret://proj/<name>` to a GitHub token; blank = public clone. Never a raw token. |

`workspace` keeps its meaning (explicit absolute path override). Its description updates to note the
new default is `<base>/<workflow_id>/<node_id>`.

Future (not in v1): `workspace_scope: "workflow" | "run"` to opt into per-run isolation.

## Security

- **No token at rest in the workflow.** Only a `secret://` ref is stored; the value is resolved at
  run time through the audited `SecretStore`, tenant/project-scoped so a ref can't cross tenants.
- **Token never leaks.** Not in env, message content, metadata, traces, or error text
  (scrub `x-access-token:...@`). It exists only in the argv/URL handed to the `git` subprocess.
- **Sandbox path.** For the isolating backend, master must add `repo_secret_ref` to the manifest's
  resolved secrets; without that the sandbox clone of a private repo fails closed (SecretNotFound),
  which is the correct behavior (no silent fallback to an unauthenticated clone).
- **Multi-tenant workspace isolation.** `<base>/<workflow_id>/<node_id>` is not tenant-scoped in the
  path, but a workflow id is unique to one tenant/project and each run VM is single-tenant, so there
  is no cross-tenant sharing in the deployed model. On shared master (dev) the temp-root fallback is
  per-process. (If we ever run multiple tenants' claude nodes on one host, prepend `tenant_id`.)

## Testing

- `_resolve_cwd` composes `<base>/<workflow_id>/<node_id>` from `ROS_CLAUDE_CODE_WORKSPACE`; honors an
  explicit `config.workspace`; falls back to temp when ids absent.
- cwd is resolved **per invocation** (two runs of the same compiled node see the same stable dir; a
  changed `ROS_CLAUDE_CODE_WORKSPACE` is picked up without recompiling).
- Repo checkout: clones when workspace empty (asserts `git clone` argv incl. `--branch`); **skips**
  when `.git` already present (clone-once); builds the `x-access-token` URL from a resolved secret;
  public clone when `repo_secret_ref` blank.
- Token scrubbing: an injected clone failure surfaces via `ClaudeCodeError` with the token redacted.
- Schema: new fields validate; a workflow using them passes `validate_workflow`; `additionalProperties`
  still rejects unknown keys.
- Plumbing: factory receives `node_id`; `CompileContext.workflow_id` populated by the compiler.

## Rollout

- Backend + schema first (this spec). UI secret-picker wiring for `repo_secret_ref` can be a
  fast-follow; until then the field accepts a `secret://` ref typed by hand.
- No image re-bake required for the node logic (ships with an api/worker deploy). The runtime's
  `ROS_CLAUDE_CODE_WORKSPACE=/workspace` export is control-plane/runtime code, also no re-bake.
- `git` is already baked into the `ros-claude-backend` image (TOOLCHAIN_STEPS), so checkout works on
  existing snapshots.

## Resolved (review)

1. Factory plumbing — back-compatible: compiler passes `node_id` only to opt-in factories. ✓
2. `workspace_scope: run` — deferred to a future toggle; v1 is persistent-per-node. ✓
3. `repo_ref` — v1 restricted to branch/tag (shallow clone); arbitrary SHA out of v1. ✓
4. Workspace GC on persistent VMs — out of scope; tie to VM lifecycle GC (GAPS G2). ✓
