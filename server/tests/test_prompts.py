"""Prompt-shape regression tests.

We don't test prompt *quality* in CI (that's the job of the LLM-fixture
regression set §12.4), but we DO assert:
  * every agent injects the shared output instructions verbatim
  * every agent's prompt mentions its own dimension name
  * the document corpus is embedded in the prompt
  * rules_findings_already_reported is surfaced so the model can de-dup
  * the technical agent injects stage-A context (code_index_hits)
"""

from __future__ import annotations

from app.agents.architecture import ArchitectureAgent
from app.agents.base import OUTPUT_INSTRUCTIONS
from app.agents.consistency import ConsistencyAgent
from app.agents.security import SecurityAgent
from app.agents.style import StyleAgent
from app.agents.technical import TechnicalAgent
from app.schema import (
    AuditRequest,
    DimensionConfig,
    Document,
    PR,
    Payload,
    Repo,
    Target,
)


def _req(dim_name: str, *, related: dict | None = None) -> AuditRequest:
    return AuditRequest(
        request_id="r",
        repo=Repo(owner="o", name="n"),
        pr=PR(number=1, base_sha="b", head_sha="h"),
        target=Target(
            name="cips",
            paths=["docs/**"],
            dimensions={dim_name: DimensionConfig(enabled=True, severity_gate="warn")},
        ),
        payload=Payload(
            index_head={
                "code_symbols_referenced": [
                    {"symbol": "some_fn", "file": "docs/cip-1.md", "line": 3}
                ],
            },
            diff={"by_kind": {}},
            changed_files=["docs/cip-1.md"],
            rules_findings=[{"rule_id": "R001", "title": "already reported"}],
            documents={"docs/cip-1.md": Document(content="line1\nline2\nline3\n")},
            related_code_excerpts=related or {},
        ),
    )


def test_all_agents_inject_shared_output_instructions():
    for cls in (ConsistencyAgent, SecurityAgent, TechnicalAgent, ArchitectureAgent, StyleAgent):
        agent = cls()
        prompt = agent.build_prompt(_req(agent.dimension), agent.prepare_inputs(_req(agent.dimension)))
        assert OUTPUT_INSTRUCTIONS in prompt, f"{cls.__name__} missing output instructions"
        # Hard-rules text should survive verbatim into the prompt — that's our
        # contract with the model.
        assert 'documents_in_scope' in prompt
        assert '1-based int' in prompt


def test_prompts_mention_dimension_name():
    for cls in (ConsistencyAgent, SecurityAgent, TechnicalAgent, ArchitectureAgent, StyleAgent):
        agent = cls()
        prompt = agent.build_prompt(_req(agent.dimension), agent.prepare_inputs(_req(agent.dimension)))
        assert agent.dimension in prompt.lower()


def test_prompt_carries_rules_findings_and_changed_files():
    agent = ConsistencyAgent()
    prompt = agent.build_prompt(_req(agent.dimension), agent.prepare_inputs(_req(agent.dimension)))
    assert "R001" in prompt  # already-reported rules finding
    assert "docs/cip-1.md" in prompt
    assert "line2" in prompt  # corpus content embedded


def test_technical_agent_injects_stage_a_and_code_index_hits():
    related = {"node/lib.rs": "pub fn some_fn() {}\n"}
    agent = TechnicalAgent()
    req = _req(agent.dimension, related=related)
    inputs = agent.prepare_inputs(req)
    assert "code_index_hits" in inputs
    assert any(h.get("symbol") == "some_fn" and h.get("found") for h in inputs["code_index_hits"])
    prompt = agent.build_prompt(req, inputs)
    assert "code_index_hits" in prompt
    assert "stage_a_findings" in prompt


def test_technical_agent_emits_stage_a_for_missing_symbol():
    """When a doc-referenced symbol is absent from related code, a T001 finding is synthesized."""
    agent = TechnicalAgent()
    req = _req(agent.dimension, related={"node/lib.rs": "pub fn unrelated() {}\n"})
    inputs = agent.prepare_inputs(req)
    rule_ids = [f["rule_id"] for f in inputs["stage_a_findings"]]
    assert "T001_symbol_not_found" in rule_ids
