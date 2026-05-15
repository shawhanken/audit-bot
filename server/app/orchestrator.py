"""Dispatch an AuditRequest across all enabled dimension agents in parallel.

Design ref: §6.2 Orchestrator. The design notes a shared prompt cache
block across dimensions; with the `claude -p` subprocess path that's not
directly exposable, so we instead let each agent build its own prompt and
rely on the model's internal caching.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from typing import Iterable

from .agents.architecture import ArchitectureAgent
from .agents.base import AgentRunResult, DimensionAgent
from .agents.consistency import ConsistencyAgent
from .agents.security import SecurityAgent
from .agents.style import StyleAgent
from .agents.technical import TechnicalAgent
from .schema import AuditRequest, AuditResponse, DimensionStatus, Finding, Totals


log = logging.getLogger(__name__)


AGENT_BY_DIM: dict[str, type[DimensionAgent]] = {
    "consistency": ConsistencyAgent,
    "security": SecurityAgent,
    "technical": TechnicalAgent,
    "architecture": ArchitectureAgent,
    "style": StyleAgent,
}


def enabled_dimensions(req: AuditRequest) -> list[str]:
    return [
        dim for dim, conf in req.target.dimensions.items()
        if conf.enabled and dim in AGENT_BY_DIM
    ]


async def orchestrate(req: AuditRequest) -> AuditResponse:
    start = time.monotonic()
    dims = enabled_dimensions(req)
    if not dims:
        return AuditResponse(
            request_id=req.request_id,
            status="ok",
            findings_by_dimension={},
            dimension_status={},
            totals=Totals(duration_ms=int((time.monotonic() - start) * 1000)),
        )

    # Run subprocess-backed agents in a thread pool so they don't block the
    # async event loop. `concurrent.futures` covers the parallel-execution
    # requirement from design §6.2.
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(dims)) as pool:
        futures = {
            dim: loop.run_in_executor(pool, _run_dim, dim, req)
            for dim in dims
        }
        results: dict[str, AgentRunResult] = {}
        for dim, fut in futures.items():
            try:
                results[dim] = await fut
            except Exception as e:  # noqa: BLE001 - single dimension failure does not stop others
                log.exception("dim %s crashed", dim)
                results[dim] = AgentRunResult(
                    status=DimensionStatus(status="degraded", reason=f"crash: {e}")
                )

    findings_by_dim: dict[str, list[Finding]] = {
        dim: r.findings for dim, r in results.items()
    }
    dim_status = {dim: r.status for dim, r in results.items()}
    overall = _overall_status(dim_status.values())
    total_ms = int((time.monotonic() - start) * 1000)
    return AuditResponse(
        request_id=req.request_id,
        status=overall,
        findings_by_dimension=findings_by_dim,
        dimension_status=dim_status,
        totals=Totals(duration_ms=total_ms),
    )


def _run_dim(dim: str, req: AuditRequest) -> AgentRunResult:
    cls = AGENT_BY_DIM[dim]
    return cls().run(req)


def _overall_status(statuses: Iterable[DimensionStatus]) -> str:
    statuses = list(statuses) if not isinstance(statuses, list) else statuses
    if not statuses:
        return "ok"
    states = {s.status for s in statuses}
    if states <= {"ok", "skipped"}:
        return "ok"
    if "ok" in states and "degraded" in states:
        return "partial"
    if states == {"degraded"} or states == {"degraded", "skipped"}:
        return "degraded"
    return "partial"
