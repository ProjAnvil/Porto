from smelt import ScriptedLLM
from smelt.then.judge import LLMJudgeExpectation
from smelt.then.expectations import NoToolCallExpectation, ToolCallExpectation

from porto_skill_eval.cases import build_cases
from porto_skill_eval.tools import build_tools, tools_for_skill


EXPECTED_SKILLS = {
    "prd-analysis",
    "subsystem-decomposition",
    "spec-generation",
    "spec-evaluation",
    "porto-memory",
}


def test_every_skill_has_semantic_positive_and_boundary_cases():
    cases = build_cases(ScriptedLLM(["unused; cases are not executed during construction"]))

    assert set(cases) == EXPECTED_SKILLS
    assert all(len(skill_cases) >= 2 for skill_cases in cases.values())
    for skill_cases in cases.values():
        for case in skill_cases:
            assert case.agent is None
            assert case.trigger is not None
            assert any(isinstance(item, LLMJudgeExpectation) for item in case.expectations)
            assert all(
                item.include_input and item.include_trace
                for item in case.expectations
                if isinstance(item, LLMJudgeExpectation)
            )


def test_memory_cases_require_and_forbid_retrieval_appropriately():
    memory_cases = build_cases(ScriptedLLM(["unused"]))["porto-memory"]
    all_expectations = [expectation for case in memory_cases for expectation in case.expectations]

    assert any(
        isinstance(item, ToolCallExpectation) and item.tool_name == "get_session_facts"
        for item in all_expectations
    )
    assert any(
        isinstance(item, ToolCallExpectation) and item.tool_name == "search_memory"
        for item in all_expectations
    )
    assert any(
        isinstance(item, NoToolCallExpectation) and item.tool_name == "search_memory"
        for item in all_expectations
    )
    assert any(
        isinstance(item, NoToolCallExpectation) and item.tool_name == "search_knowledgebase"
        for item in all_expectations
    )
    assert any(
        isinstance(item, NoToolCallExpectation) and item.tool_name == "search_memory"
        for item in all_expectations
    )


def test_registered_tools_are_only_fixture_backed_read_operations():
    tools = build_tools()

    assert {item.name for item in tools} == {
        "get_prd_text",
        "search_knowledgebase",
        "list_subsystems",
        "get_subsystem",
        "search_memory",
        "get_session_facts",
    }
    assert all(item.name.startswith(("get_", "list_", "search_")) for item in tools)
    assert all("fixture" in item.description.lower() for item in tools)


def test_explicit_source_requests_bound_tool_use_and_forbid_unrelated_retrieval():
    cases = build_cases(ScriptedLLM(["unused"]))

    def forbidden_names(case):
        return {
            item.tool_name
            for item in case.expectations
            if isinstance(item, NoToolCallExpectation)
        }

    prd_case = cases["prd-analysis"][0]
    assert {"get_prd_text", "search_knowledgebase", "search_memory", "get_session_facts"} <= forbidden_names(prd_case)
    assert any(
        isinstance(item, ToolCallExpectation) and item.tool_name == "search_knowledgebase"
        for item in cases["spec-generation"][1].expectations
    )


def test_skill_tool_inventory_exposes_only_relevant_fixture_read_tools():
    assert {tool.name for tool in tools_for_skill("prd-analysis")} == {
        "get_prd_text",
        "search_knowledgebase",
    }
    assert tools_for_skill("subsystem-decomposition") == []
    assert {tool.name for tool in tools_for_skill("spec-generation")} == {"search_knowledgebase"}
    assert tools_for_skill("spec-evaluation") == []
    assert {tool.name for tool in tools_for_skill("porto-memory")} == {
        "get_session_facts",
        "search_memory",
    }
