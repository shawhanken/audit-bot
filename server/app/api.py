"""FastAPI HTTP layer.

Design ref: §6.2. Endpoints:
  POST /v1/audit
  GET  /v1/audit/{request_id}
  GET  /v1/health
  GET  /v1/budget/{repo}
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import budget as _budget
from . import dedup as _dedup
from . import orchestrator
from . import storage as _storage
from .auth import TokenStore
from .metrics import Metrics
from .notifier import BreachEvent, LoggingNotifier, Notifier
from .schema import AuditRequest, AuditResponse


router = APIRouter(prefix="/v1")


def _state(request: Request) -> "ServerState":
    return request.app.state.audit_state


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    return authorization[len("Bearer "):]


def _require_token(authorization: Annotated[str | None, Header()] = None) -> str:
    """Three modes, checked in order:
      1. `DOC_AUDIT_SERVER_TOKEN` env var set — match exactly (legacy / dev).
      2. Token present in the SQLite store — accepted; binding enforced later
         at the endpoint level against `request.repo`.
      3. No env var AND no DB binding — open mode (local dev convenience).
    """
    expected_env = os.getenv("DOC_AUDIT_SERVER_TOKEN", "")
    if expected_env:
        token = _extract_bearer(authorization)
        if token != expected_env:
            raise HTTPException(status_code=401, detail="invalid token")
        return token
    if authorization:
        # If a bearer header was sent, we accept it for now; per-repo binding
        # is enforced in `_require_repo_binding`.
        return _extract_bearer(authorization)
    return ""


def _require_repo_binding(request: Request, repo_full: str, token: str) -> None:
    """When a token is present and the server has any registered bindings,
    the token must belong to `repo_full`. Pure-env-token mode (legacy) and
    open mode skip this check.
    """
    if not token or os.getenv("DOC_AUDIT_SERVER_TOKEN"):
        return
    store: TokenStore = request.app.state.audit_state.tokens
    binding = store.lookup(token)
    if binding is None:
        # Unknown token + no env var configured → reject explicitly rather
        # than silently allow.
        raise HTTPException(status_code=401, detail="unknown token")
    if binding.repo != repo_full:
        raise HTTPException(
            status_code=403,
            detail=f"token bound to {binding.repo}, not {repo_full}",
        )


@router.get("/health")
async def health(request: Request) -> dict:
    st = _state(request)
    return {
        "status": "ok",
        "cache_size": st.cache.size(),
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> str:
    return _state(request).metrics.render()


@router.post("/audit", response_model=AuditResponse)
async def audit(
    body: AuditRequest,
    request: Request,
    token: str = Depends(_require_token),
) -> AuditResponse:
    st = _state(request)
    repo_full = body.repo.full_name
    _require_repo_binding(request, repo_full, token)

    # Hard budget gate (design §6.2): refuse new requests when over cap.
    budget = st.budget.state(repo_full)
    if budget.hard_breach:
        st.notifier.notify(BreachEvent(
            repo=repo_full,
            spent_usd=budget.spent_usd,
            cap_usd=budget.cap_usd,
            kind="hard",
        ))
        raise HTTPException(
            status_code=503,
            detail={"reason": "budget_exhausted", "remaining_usd": budget.remaining_usd},
        )

    # Negative cache short-circuit. Design §6.2 specifies two parallel keys:
    #   audit_result:<repo>:<head_sha>:<target>  — exact PR-state re-runs
    #   negative:<hash(diff)>                    — same diff via different SHA
    #                                              (force-push with no real
    #                                              changes still costs $)
    cache_key = f"audit_result:{repo_full}:{body.pr.head_sha}:{body.target.name}"
    cached = st.cache.get(cache_key)
    if cached is not None:
        return cached
    diff_key = f"negative:{repo_full}:{body.target.name}:{_diff_hash(body)}"
    cached = st.cache.get(diff_key)
    if cached is not None:
        # Re-issue under the new head_sha key so subsequent requests on this
        # SHA take the faster path.
        st.cache.set(cache_key, cached, ttl_seconds=300)
        return cached

    response = await orchestrator.orchestrate(body)
    st.metrics.incr("doc_audit_requests_total", labels={
        "repo": repo_full, "status": response.status,
    })
    st.metrics.observe(
        "doc_audit_duration_ms", response.totals.duration_ms,
        labels={"repo": repo_full},
    )
    for dim, status in response.dimension_status.items():
        st.metrics.incr("doc_audit_dimension_runs_total", labels={
            "dimension": dim, "status": status.status,
        })

    # Annotate findings with history (cross-PR dedup).
    for dim, findings in list(response.findings_by_dimension.items()):
        response.findings_by_dimension[dim] = _dedup.annotate_history(
            findings, repo=repo_full, store=st.history
        )

    # Update budget. The skeleton doesn't have real token accounting, so we
    # bill a flat cost-per-dimension as a placeholder; the design's response
    # totals are still emitted for clients to read.
    cost = 0.0
    for status in response.dimension_status.values():
        if status.status == "ok":
            cost += 0.05
    response.totals.cost_usd = cost
    st.budget.record(
        repo=repo_full,
        request_id=body.request_id,
        pr_number=body.pr.number,
        tokens_input=0,
        tokens_output=0,
        tokens_cached_read=0,
        cost_usd=cost,
        duration_ms=response.totals.duration_ms,
    )
    budget_state = st.budget.state(repo_full)
    response.remaining_budget_usd = budget_state.remaining_usd
    st.metrics.set_gauge(
        "doc_audit_budget_remaining_usd", response.remaining_budget_usd,
        labels={"repo": repo_full},
    )
    if budget_state.soft_breach and not budget_state.hard_breach:
        st.notifier.notify(BreachEvent(
            repo=repo_full,
            spent_usd=budget_state.spent_usd,
            cap_usd=budget_state.cap_usd,
            kind="soft",
        ))

    st.history.save_audit_run(
        request_id=body.request_id,
        repo=repo_full,
        pr_number=body.pr.number,
        target=body.target.name,
        response=response,
    )
    st.cache.set(cache_key, response, ttl_seconds=300)
    # Negative cache: 1 day TTL per design table. Cheap to keep — if the diff
    # body really is identical we want the same answer.
    st.cache.set(diff_key, response, ttl_seconds=86_400)
    return response


def _diff_hash(req) -> str:
    """Stable hash of the parts of the payload that determine the LLM input.

    Includes diff, changed_files, and rules_findings (so a new rule firing
    against an old diff invalidates the cache). Excludes documents content —
    if the changed_files set is the same and the diff is the same, document
    content reuse is the whole point of this cache.
    """
    parts = {
        "diff": req.payload.diff,
        "changed_files": sorted(req.payload.changed_files),
        "rules_findings_ids": sorted(
            f.get("finding_id", "") for f in req.payload.rules_findings or []
        ),
        "enabled_dims": sorted(
            d for d, c in req.target.dimensions.items() if c.enabled
        ),
    }
    return hashlib.sha1(
        json.dumps(parts, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


@router.get("/audit/{request_id}")
async def get_audit(request_id: str, request: Request, _token: str = Depends(_require_token)) -> dict:
    st = _state(request)
    row = st.history.get_audit_run(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row


@router.get("/budget/{owner}/{name}")
async def get_budget(owner: str, name: str, request: Request, _token: str = Depends(_require_token)) -> dict:
    st = _state(request)
    s = st.budget.state(f"{owner}/{name}")
    return {
        "repo": s.repo,
        "spent_usd": s.spent_usd,
        "cap_usd": s.cap_usd,
        "remaining_usd": s.remaining_usd,
        "soft_breach": s.soft_breach,
        "hard_breach": s.hard_breach,
    }


class ServerState:
    """Bundled stateful dependencies, attached to `app.state` at startup."""

    def __init__(self, db_path: str, *, notifier: Notifier | None = None):
        from .cache import TTLCache
        self.history = _storage.HistoryStore(db_path)
        self.budget = _budget.BudgetLedger(db_path)
        self.tokens = TokenStore(db_path)
        self.cache = TTLCache()
        self.metrics = Metrics()
        self.notifier: Notifier = notifier or LoggingNotifier()


def build_app(*, db_path: str | None = None, notifier: Notifier | None = None) -> FastAPI:
    app = FastAPI(title="Doc Audit Service", version="0.1.0")
    db = db_path or os.getenv("DOC_AUDIT_DB", "doc_audit.sqlite3")
    app.state.audit_state = ServerState(db, notifier=notifier)
    app.include_router(router)
    return app
