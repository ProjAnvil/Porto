import json
from pathlib import Path

from smelt import Tool, tool


FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


@tool(description="Read the fixture PRD; this fixture-only tool never accesses Porto services.")
def get_prd_text() -> str:
    return _read_fixture("prd/payment_platform.md")


@tool(description="Search fixture knowledge-base conventions; this tool never uses a live index.")
def search_knowledgebase(query: str) -> str:
    del query
    return _read_fixture("knowledgebase/payment_conventions.md")


@tool(description="List fixture subsystems; this tool never reads workflow state.")
def list_subsystems() -> str:
    return "payment-service: authorization, refund, settlement; risk-service: fraud checks; notification-service: results."


@tool(description="Read a fixture subsystem; this tool never reads workflow state.")
def get_subsystem(name: str) -> str:
    if name == "payment-service":
        return "Owns payment authorization, refunds, and settlement."
    if name == "risk-service":
        return "Evaluates high-risk transactions before authorization."
    return "No fixture subsystem matches that name."


@tool(description="Search fixture conversation memory; this tool never accesses user conversations.")
def search_memory(query: str) -> str:
    del query
    return _read_fixture("memory/prior_decisions.md")


@tool(description="Read fixture session facts; this tool never accesses a live session.")
def get_session_facts() -> str:
    return json.dumps(json.loads(_read_fixture("session_facts.json")), ensure_ascii=False)


def build_tools() -> list[Tool]:
    """Return only in-memory fixture-backed read tools."""
    return [
        get_prd_text,
        search_knowledgebase,
        list_subsystems,
        get_subsystem,
        search_memory,
        get_session_facts,
    ]


_SKILL_TOOL_NAMES = {
    "prd-analysis": {"get_prd_text", "search_knowledgebase"},
    "subsystem-decomposition": set(),
    "spec-generation": {"search_knowledgebase"},
    "spec-evaluation": set(),
    "porto-memory": {"get_session_facts", "search_memory"},
}


def tools_for_skill(skill_name: str) -> list[Tool]:
    """Expose only fixture readers relevant to the selected skill."""
    if skill_name not in _SKILL_TOOL_NAMES:
        raise ValueError(f"Unknown skill {skill_name!r}")
    allowed = _SKILL_TOOL_NAMES[skill_name]
    return [tool_item for tool_item in build_tools() if tool_item.name in allowed]
