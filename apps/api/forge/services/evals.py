"""EvalService - run a dataset against a workflow and score it.

Scoring modes (dataset-wide `score_mode`, still supported):
- contains / exact / regex - deterministic, offline, no model spend.
- numeric - number-in-answer within an absolute/relative tolerance of the expected number.
- json - parse the answer as JSON and match expected fields (subset or exact).
- embedding - cosine similarity of the project embedder's vectors >= a threshold.
- judge - an LLM grades whether the answer satisfies the expected behavior (reasons first,
  and returns an explicit "unavailable" status - NOT a silent substring fallback - when no
  real judge model is available, so pass rates never look better than they are).

Per item you may instead supply an ASSERTION LIST (`item["assertions"] = [{type,...}]`)
combined with AND (default) or OR (`item["assert"] = "any"`), mixing any of the above.

Runs execute with BOUNDED CONCURRENCY (finding F1) and every run is PERSISTED as an
EvalRun + per-item EvalResults (finding F2) for history, per-example diffing, and an
optional publish-time regression gate that compares the new pass rate to the previous run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from sqlalchemy import select

from forge.config import settings
from forge.db.base import SessionLocal
from forge.db.scoping import set_current_tenant
from forge.models import Dataset, EvalResult, EvalRun
from forge.services.dispatch import dispatch_message
from forge.services.quota import QuotaExceeded, check_run_quota
from forge.services.runs import RunService

log = logging.getLogger("forge.evals")

# A single eval run can fire one billable model run per item; cap it so a huge dataset
# can't kick off thousands of runs from one request (audit F11).
_MAX_EVAL_ITEMS = 1000

# Bounded fan-out (finding F1): run this many items at once instead of a sequential await
# loop. Each item's model run still passes through RunService's per-tenant concurrency guard,
# so this stays well under max_concurrent_runs_per_tenant. Overridable via settings.eval_concurrency.
_DEFAULT_EVAL_CONCURRENCY = 5

# A pass-rate drop this small (floating-point / one-flaky-item noise) does NOT trip the gate.
_REGRESSION_EPSILON = 1e-9

# Reason first, THEN decide (finding F4): a boolean-first schema makes weak models commit to an
# answer before reasoning. Kept minimal so structured-output works across providers.
_JUDGE_SCHEMA = {
    "title": "ItemJudgement",
    "type": "object",
    "properties": {
        "reasoning": {"type": "string", "description": "Briefly compare the answer to the expected behavior."},
        "passed": {"type": "boolean", "description": "true only if the answer satisfies the expected behavior."},
    },
    "required": ["reasoning", "passed"],
}

_JUDGE_RUBRIC = (
    "PASS only if the ANSWER is correct and satisfies the EXPECTED behavior for the INPUT. "
    "FAIL if it is wrong, empty, off-topic, contradicts the expected behavior, or omits a "
    "required part. Wording differences that preserve the meaning still PASS."
)


# --- deterministic scorers ---

def _score_deterministic(mode: str, answer: str, expected: str) -> bool:
    a, e = (answer or "").strip(), (expected or "").strip()
    if mode == "exact":
        return a == e
    if mode == "regex":
        try:
            return re.search(expected or "", answer or "") is not None
        except re.error:
            return False
    # default: contains (case-insensitive)
    return e.lower() in a.lower()


_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def _first_number(text: str) -> float | None:
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _score_numeric(answer: str, expected: str, *, tolerance: float = 0.0, rel_tolerance: float = 0.0) -> tuple[bool, float | None]:
    """First number in the answer within max(abs, |expected|*rel) tolerance of the expected
    number. Returns (passed, absolute_error)."""
    a, e = _first_number(answer), _first_number(expected)
    if a is None or e is None:
        return False, None
    diff = abs(a - e)
    tol = max(float(tolerance or 0.0), abs(e) * float(rel_tolerance or 0.0))
    return diff <= tol, diff


def _json_loads(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _subset_match(expected, actual) -> bool:
    """Deep partial match: every key/value in `expected` is present (recursively) in `actual`."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _subset_match(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) > len(actual):
            return False
        return all(_subset_match(ev, av) for ev, av in zip(expected, actual, strict=False))
    return expected == actual


def _score_json(answer: str, expected, *, mode: str = "subset") -> bool:
    exp = expected if not isinstance(expected, str) else _json_loads(expected)
    act = _json_loads(answer)
    if exp is None or act is None:
        return False
    return act == exp if mode == "exact" else _subset_match(exp, act)


def _is_real_judge(model) -> bool:
    """False for None and for the offline `fake:` chat model, so a judge run on an offline
    model reports "unavailable" instead of grading against a canned string (finding F4)."""
    if model is None:
        return False
    try:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        return not isinstance(model, GenericFakeChatModel)
    except Exception:  # noqa: BLE001
        return True


class EvalService:
    @staticmethod
    async def list(session, tenant_id: str, project_id: str) -> list[Dataset]:
        rows = await session.execute(
            select(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.project_id == project_id)
        )
        return list(rows.scalars())

    @staticmethod
    async def get(session, tenant_id: str, dataset_id: str) -> Dataset | None:
        return (await session.execute(
            select(Dataset).where(Dataset.tenant_id == tenant_id, Dataset.id == dataset_id)
        )).scalar_one_or_none()

    @staticmethod
    async def create(session, tenant_id, project_id, *, name, workflow_id=None, score_mode="contains", items=None) -> Dataset:
        ds = Dataset(tenant_id=tenant_id, project_id=project_id, name=name, workflow_id=workflow_id,
                     score_mode=score_mode, items=items or [])
        session.add(ds)
        await session.commit()
        await session.refresh(ds)
        return ds

    @staticmethod
    async def update(session, ds: Dataset, *, name, workflow_id=None, score_mode="contains", items=None) -> Dataset:
        ds.name = name
        ds.workflow_id = workflow_id
        ds.score_mode = score_mode
        ds.items = items or []
        await session.commit()
        await session.refresh(ds)
        return ds

    @staticmethod
    async def delete(session, ds: Dataset) -> None:
        await session.delete(ds)
        await session.commit()

    @staticmethod
    async def history(session, tenant_id: str, dataset_id: str, limit: int = 50) -> list[EvalRun]:
        rows = await session.execute(
            select(EvalRun).where(EvalRun.tenant_id == tenant_id, EvalRun.dataset_id == dataset_id)
            .order_by(EvalRun.created_at.desc()).limit(limit)
        )
        return list(rows.scalars())

    @staticmethod
    async def results(session, tenant_id: str, eval_run_id: str) -> list[EvalResult]:
        rows = await session.execute(
            select(EvalResult).where(EvalResult.tenant_id == tenant_id, EvalResult.eval_run_id == eval_run_id)
            .order_by(EvalResult.item_index.asc())
        )
        return list(rows.scalars())

    # --- scoring ---

    @staticmethod
    async def _assertion(a: dict, answer: str, item_expected: str, *, judge_model, embedder, inp: str) -> dict:
        """Evaluate one assertion → {passed, score, status, detail}."""
        typ = (a.get("type") or "contains").lower()
        expected = a.get("expected", a.get("value", item_expected))
        exp_str = expected if isinstance(expected, str) else json.dumps(expected, default=str)
        passed, score, status, detail = False, None, "scored", ""
        if typ in ("contains", "exact", "regex"):
            passed = _score_deterministic(typ, answer, exp_str)
        elif typ == "numeric":
            passed, err = _score_numeric(answer, exp_str, tolerance=a.get("tolerance", 0.0), rel_tolerance=a.get("rel_tolerance", 0.0))
            score = err
            if err is None:
                detail = "no comparable number found"
        elif typ in ("json", "json_match", "field_match"):
            passed = _score_json(answer, expected, mode=a.get("mode", "subset"))
        elif typ in ("embedding", "similarity"):
            threshold = float(a.get("threshold", 0.8))
            if embedder is None:
                status, detail = "unavailable", "no embedder available"
            else:
                from forge.knowledge.embeddings import cosine
                va, ve = await embedder.aembed_query(answer or ""), await embedder.aembed_query(exp_str)
                score = cosine(va, ve)
                passed = score >= threshold
        elif typ == "judge":
            passed, reason, jstatus = await EvalService._judge(judge_model, inp, exp_str, answer, rubric=a.get("rubric"))
            status, detail = jstatus, reason or ""
        else:
            status, detail = "error", f"unknown assertion type {typ!r}"
        if a.get("negate") and status == "scored":
            passed = not passed
        return {"type": typ, "passed": bool(passed), "score": score, "status": status, "detail": detail}

    @staticmethod
    async def _score_item(dataset: Dataset, item: dict, answer: str, *, judge_model, embedder) -> dict:
        """Score one produced answer. Per-item assertion list (AND/OR) if present, else the
        dataset-wide score_mode. Returns {passed, score, status, reason, checks}."""
        inp, expected = item.get("input", ""), item.get("expected", "")
        assertions = item.get("assertions")
        if not assertions:
            # A dataset-wide mode is just a single implicit assertion of that type.
            assertions = [{"type": dataset.score_mode or "contains", "expected": expected}]
        combine = (item.get("assert") or item.get("combine") or "all").lower()
        checks = [await EvalService._assertion(a, answer, expected, judge_model=judge_model, embedder=embedder, inp=inp)
                  for a in assertions]
        scored = [c for c in checks if c["status"] == "scored"]
        inconclusive = [c for c in checks if c["status"] != "scored"]
        # An inconclusive check (unavailable/error judge or embedder) must not read as a pass.
        if not scored and inconclusive:
            status = inconclusive[0]["status"]
            reason = "; ".join(c["detail"] for c in inconclusive if c["detail"]) or status
            return {"passed": False, "score": None, "status": status, "reason": reason, "checks": checks}
        results = [c["passed"] for c in scored]
        passed = any(results) if combine in ("any", "or") else all(results)
        # If an OR already passed, inconclusive siblings don't matter; for AND they block it.
        if inconclusive and combine not in ("any", "or"):
            passed = False
        score = next((c["score"] for c in checks if c["score"] is not None), None)
        reason = "; ".join(f"{c['type']}:{'pass' if c['passed'] else c['status'] if c['status'] != 'scored' else 'fail'}" for c in checks)
        return {"passed": bool(passed), "score": score,
                "status": "scored" if scored else "unavailable", "reason": reason, "checks": checks}

    @staticmethod
    async def _judge(model, inp: str, expected: str, answer: str, *, rubric: str | None = None) -> tuple[bool, str, str]:
        """Return (passed, reason, status). status: scored | unavailable | error. On no real
        judge model or a call error we DO NOT fall back to substring matching (finding F4)."""
        if not _is_real_judge(model):
            return False, "judge model unavailable (offline/fake model) - result inconclusive, not a fail", "unavailable"
        prompt = (
            "You are grading an AI assistant's answer.\n" + (rubric or _JUDGE_RUBRIC) + "\n\n"
            f"INPUT:\n{inp}\n\nEXPECTED:\n{expected}\n\nANSWER:\n{answer}"
        )
        try:
            res = await model.with_structured_output(_JUDGE_SCHEMA).ainvoke(prompt)
            passed = bool(res.get("passed") if isinstance(res, dict) else getattr(res, "passed", False))
            reason = (res.get("reasoning") if isinstance(res, dict) else getattr(res, "reasoning", None)) or ""
            return passed, reason, "scored"
        except Exception as e:  # noqa: BLE001
            return False, f"judge error: {e}", "error"

    # --- run ---

    @staticmethod
    async def _execute_and_score(run_service: RunService, dataset: Dataset, idx: int, item: dict, *,
                                 judge_model, embedder, end_user, run_context, sem) -> dict:
        """Run ONE item against the workflow and score it, timing the run for a per-item latency.

        Shared by the batch `run` and the streaming `stream` so both report identical
        {index,input,expected,answer,passed,status,reason,checks,tokens,cost,latency_ms}. A
        failing run is caught and returned as a `run_failed` result so it never aborts the batch."""
        inp, expected = item.get("input", ""), item.get("expected", "")
        t0 = time.monotonic()
        async with sem:
            # dispatch_message opens its OWN sessions, so items run safely in parallel. end_user /
            # run_context make each item act on behalf of the launching user (auth like the Playground).
            try:
                res = await dispatch_message(
                    run_service, tenant_id=dataset.tenant_id, project_id=dataset.project_id,
                    workflow_id=dataset.workflow_id, text=inp, end_user=end_user, run_context=run_context,
                )
            except Exception as e:  # noqa: BLE001 - one failing run must not abort the eval (F11)
                log.warning("eval item %s failed for dataset %s: %s", idx, dataset.id, e)
                return {"index": idx, "input": inp, "expected": expected, "answer": "", "passed": False,
                        "score": None, "status": "run_failed", "reason": f"run failed: {e}", "checks": [],
                        "tokens": 0, "cost": 0.0, "latency_ms": int((time.monotonic() - t0) * 1000)}
        latency_ms = int((time.monotonic() - t0) * 1000)
        answer = res.get("answer", "") or ""
        if res.get("error"):
            sc = {"passed": False, "score": None, "status": "run_failed", "reason": res["error"], "checks": []}
        else:
            sc = await EvalService._score_item(dataset, item, answer, judge_model=judge_model, embedder=embedder)
        return {"index": idx, "input": inp, "expected": expected, "answer": answer,
                "tokens": int(res.get("total_tokens") or 0), "cost": float(res.get("total_cost_usd") or 0.0),
                "latency_ms": latency_ms, **sc}

    @staticmethod
    def _summarize(results: list[dict]) -> dict:
        """Aggregate scored per-item results into the run summary numbers."""
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        inconclusive = sum(1 for r in results if r["status"] in ("unavailable", "error", "run_failed"))
        return {"total": total, "passed": passed,
                "inconclusive": inconclusive, "rate": (passed / total) if total else 0.0,
                "tokens": sum(r["tokens"] for r in results), "cost": sum(r["cost"] for r in results)}

    @staticmethod
    def _clean_results(results: list[dict]) -> list[dict]:
        """The per-item fields the API/UI consumes (internals stay on the persisted EvalResult)."""
        return [{k: r.get(k) for k in ("input", "expected", "answer", "passed", "score", "status",
                                       "reason", "checks", "tokens", "cost", "latency_ms")}
                for r in results]

    @staticmethod
    async def run(session, run_service: RunService, dataset: Dataset, *, judge_model=None,
                  concurrency: int | None = None, regression_gate: bool = False,
                  min_pass_rate: float | None = None, end_user: dict | None = None,
                  run_context: dict | None = None) -> dict:
        """Execute every item against the dataset's workflow, score it, and persist the run.

        `regression_gate` flags (does not block) when the new pass rate drops below the
        dataset's previous rate; `min_pass_rate` flags an absolute floor. The publish flow
        can read `summary["regression"]` to decide whether to block.

        `end_user` / `run_context` let each item run act on behalf of the launching user, just
        like the Playground: a per-user auth provider then resolves that user's connected
        credential (or a forwarded inline token) so tool-using items authenticate."""
        workflow_id = dataset.workflow_id
        if not workflow_id:
            return {"error": "dataset has no workflow bound"}
        # Eval runs are billable model calls - gate the whole batch on the tenant's daily quota.
        try:
            await check_run_quota(session, dataset.tenant_id, project_id=dataset.project_id)
        except QuotaExceeded as e:
            return {"error": e.message}

        all_items = dataset.items or []
        items = all_items[:_MAX_EVAL_ITEMS]
        truncated = len(all_items) > _MAX_EVAL_ITEMS

        # Resolve the project embedder ONCE (up front, on this session) when any scorer needs
        # it - never inside the concurrent tasks, which must not share `session`.
        embedder = None
        if EvalService._needs_embedder(dataset, items):
            try:
                from forge.services.knowledge import KnowledgeService
                embedder = await KnowledgeService.embedder_for_project(session, dataset.tenant_id, dataset.project_id)
            except Exception as e:  # noqa: BLE001 - embedding assertions then report "unavailable"
                log.warning("eval embedder unavailable for dataset %s: %s", dataset.id, e)

        limit = concurrency or getattr(settings, "eval_concurrency", None) or _DEFAULT_EVAL_CONCURRENCY
        sem = asyncio.Semaphore(max(1, int(limit)))

        gathered = await asyncio.gather(*[
            EvalService._execute_and_score(run_service, dataset, i, it, judge_model=judge_model,
                                           embedder=embedder, end_user=end_user, run_context=run_context, sem=sem)
            for i, it in enumerate(items)
        ])
        results = sorted(gathered, key=lambda r: r["index"])

        agg = EvalService._summarize(results)
        rate = agg["rate"]

        prev = dataset.last_pass_rate
        regressed = bool(regression_gate and prev is not None and rate < prev - _REGRESSION_EPSILON)
        below_floor = bool(min_pass_rate is not None and rate < float(min_pass_rate))

        eval_run = await EvalService._persist(
            session, dataset, results, total=agg["total"], passed=agg["passed"], rate=rate, prev=prev,
            regressed=regressed, tokens=agg["tokens"], cost=agg["cost"],
            meta={"truncated": truncated, "items_skipped": max(0, len(all_items) - _MAX_EVAL_ITEMS),
                  "inconclusive": agg["inconclusive"], "regression_gate": regression_gate,
                  "min_pass_rate": min_pass_rate, "below_floor": below_floor},
        )
        dataset.last_pass_rate = rate
        await session.commit()

        summary = {"total": agg["total"], "passed": agg["passed"], "pass_rate": round(rate, 4),
                   "inconclusive": agg["inconclusive"], "tokens": agg["tokens"], "cost_usd": round(agg["cost"], 6),
                   "eval_run_id": eval_run.id if eval_run else None}
        if truncated:
            summary["truncated"] = True
            summary["items_skipped"] = len(all_items) - _MAX_EVAL_ITEMS
        if regression_gate or min_pass_rate is not None:
            summary["regression"] = {"prev_pass_rate": prev, "regressed": regressed,
                                     "below_floor": below_floor, "blocked": regressed or below_floor}
        return {"summary": summary, "results": EvalService._clean_results(results)}

    @staticmethod
    async def stream(run_service: RunService, dataset: Dataset, *, judge_model=None,
                     concurrency: int | None = None, regression_gate: bool = False,
                     min_pass_rate: float | None = None, end_user: dict | None = None,
                     run_context: dict | None = None):
        """Async generator mirroring `run`, but yielding {event, data} frames AS EACH ITEM
        FINISHES so the UI can show live progress instead of blocking on the whole batch:

          - start : {total, items:[{index,input,expected}]}  (render all rows immediately)
          - item  : one scored result {index,passed,status,...,latency_ms,tokens,cost} per case
          - done  : {summary, results}                        (final numbers, run persisted)
          - error : {error}                                   (pre-flight failure, e.g. no quota)

        Runs use their OWN sessions (dispatch_message); the final persist opens a fresh session
        so nothing depends on a request-scoped session staying open across the SSE response."""
        if not dataset.workflow_id:
            yield {"event": "error", "data": {"error": "dataset has no workflow bound"}}
            return
        set_current_tenant(dataset.tenant_id)  # RLS binding for the fresh sessions below
        try:
            async with SessionLocal() as s:
                await check_run_quota(s, dataset.tenant_id, project_id=dataset.project_id)
        except QuotaExceeded as e:
            yield {"event": "error", "data": {"error": e.message}}
            return

        all_items = dataset.items or []
        items = all_items[:_MAX_EVAL_ITEMS]
        truncated = len(all_items) > _MAX_EVAL_ITEMS

        embedder = None
        if EvalService._needs_embedder(dataset, items):
            try:
                from forge.services.knowledge import KnowledgeService
                async with SessionLocal() as s:
                    embedder = await KnowledgeService.embedder_for_project(s, dataset.tenant_id, dataset.project_id)
            except Exception as e:  # noqa: BLE001
                log.warning("eval embedder unavailable for dataset %s: %s", dataset.id, e)

        yield {"event": "start", "data": {
            "total": len(items), "truncated": truncated,
            "items": [{"index": i, "input": it.get("input", ""), "expected": it.get("expected", "")}
                      for i, it in enumerate(items)],
        }}

        limit = concurrency or getattr(settings, "eval_concurrency", None) or _DEFAULT_EVAL_CONCURRENCY
        sem = asyncio.Semaphore(max(1, int(limit)))
        tasks = [asyncio.ensure_future(
            EvalService._execute_and_score(run_service, dataset, i, it, judge_model=judge_model,
                                           embedder=embedder, end_user=end_user, run_context=run_context, sem=sem))
            for i, it in enumerate(items)]

        results: list[dict] = []
        # as_completed yields in FINISH order (fast cases first) while the semaphore still bounds
        # how many run at once - so the client sees each row resolve the moment its run returns.
        for fut in asyncio.as_completed(tasks):
            r = await fut
            results.append(r)
            # Keep `index` on the wire so the client can map each result to its row (the shared
            # _clean_results drops it, since the batch API returns results already index-ordered).
            yield {"event": "item", "data": {"index": r["index"], **EvalService._clean_results([r])[0]}}

        results.sort(key=lambda r: r["index"])
        agg = EvalService._summarize(results)
        rate = agg["rate"]

        # Persist the run + update the dataset pass-rate in a fresh session (the request session
        # may already be closing under the streaming response).
        eval_run_id = None
        prev = dataset.last_pass_rate
        regressed = bool(regression_gate and prev is not None and rate < prev - _REGRESSION_EPSILON)
        below_floor = bool(min_pass_rate is not None and rate < float(min_pass_rate))
        try:
            async with SessionLocal() as s:
                ds = await s.get(Dataset, dataset.id)
                if ds is not None:
                    eval_run = await EvalService._persist(
                        s, ds, results, total=agg["total"], passed=agg["passed"], rate=rate, prev=prev,
                        regressed=regressed, tokens=agg["tokens"], cost=agg["cost"],
                        meta={"truncated": truncated, "items_skipped": max(0, len(all_items) - _MAX_EVAL_ITEMS),
                              "inconclusive": agg["inconclusive"], "regression_gate": regression_gate,
                              "min_pass_rate": min_pass_rate, "below_floor": below_floor},
                    )
                    eval_run_id = eval_run.id if eval_run else None
                    ds.last_pass_rate = rate
                    await s.commit()
        except Exception as e:  # noqa: BLE001 - a persist failure must not lose the live report
            log.warning("failed to persist streamed eval run for dataset %s: %s", dataset.id, e)

        summary = {"total": agg["total"], "passed": agg["passed"], "pass_rate": round(rate, 4),
                   "inconclusive": agg["inconclusive"], "tokens": agg["tokens"], "cost_usd": round(agg["cost"], 6),
                   "eval_run_id": eval_run_id}
        if truncated:
            summary["truncated"] = True
            summary["items_skipped"] = len(all_items) - _MAX_EVAL_ITEMS
        if regression_gate or min_pass_rate is not None:
            summary["regression"] = {"prev_pass_rate": prev, "regressed": regressed,
                                     "below_floor": below_floor, "blocked": regressed or below_floor}
        yield {"event": "done", "data": {"summary": summary, "results": EvalService._clean_results(results)}}

    @staticmethod
    def _needs_embedder(dataset: Dataset, items: list[dict]) -> bool:
        if (dataset.score_mode or "") in ("embedding", "similarity"):
            return True
        for it in items:
            for a in (it.get("assertions") or []):
                if (a.get("type") or "").lower() in ("embedding", "similarity"):
                    return True
        return False

    @staticmethod
    async def _persist(session, dataset: Dataset, results: list[dict], *, total, passed, rate, prev,
                       regressed, tokens, cost, meta) -> EvalRun | None:
        """Write the EvalRun + per-item EvalResults. Best-effort: a persistence failure must
        not lose the (already-computed) report the caller is about to return."""
        try:
            eval_run = EvalRun(
                tenant_id=dataset.tenant_id, project_id=dataset.project_id, dataset_id=dataset.id,
                workflow_id=dataset.workflow_id, score_mode=dataset.score_mode or "contains", status="done",
                total=total, passed=passed, pass_rate=rate, prev_pass_rate=prev, regressed=regressed,
                total_tokens=tokens, total_cost_usd=cost, meta=meta,
            )
            session.add(eval_run)
            await session.flush()
            for r in results:
                session.add(EvalResult(
                    tenant_id=dataset.tenant_id, eval_run_id=eval_run.id, item_index=r["index"],
                    input=str(r.get("input", "")), expected=str(r.get("expected", "")),
                    answer=str(r.get("answer", "")), passed=r["passed"], score=r.get("score"),
                    status=r.get("status", "scored"), reason=r.get("reason"), checks=r.get("checks") or [],
                ))
            await session.flush()
            return eval_run
        except Exception as e:  # noqa: BLE001
            log.warning("failed to persist eval run for dataset %s: %s", dataset.id, e)
            return None
