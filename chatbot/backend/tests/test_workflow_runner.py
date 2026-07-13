from unittest.mock import MagicMock

from porto_chatbot.workflow_runner import CHECKPOINTS, STEPS, WorkflowRunner


def _agent():
    return MagicMock()


def _state(current_step=None):
    return {
        "workflow_id": "w1",
        "project_name": "p",
        "prd_text": "prd",
        "sources": [],
        "understanding": "",
        "subsystems": [],
        "specs": {},
        "evaluation": {},
        "steps": [],
        "top_k": 6,
        "current_step": current_step,
    }


def test_stops_at_understand_from_none():
    """从 current_step=None 跑,retrieve 自动过,understand 是 checkpoint → 停在 understand。"""
    import porto_chatbot.workflow_runner as wr

    agent = _agent()
    calls = []

    def fake_retrieve(agent_, state):
        state["sources"] = ["src"]
        state["current_step"] = "retrieve"
        calls.append("retrieve")
        return state

    def fake_understand(agent_, state):
        state["understanding"] = "U"
        state["current_step"] = "understand"
        calls.append("understand")
        return state

    wr.retrieve_node.retrieve_knowledge = fake_retrieve
    wr.understand_node.understand_prd = fake_understand

    state = WorkflowRunner.run_to_next_checkpoint(agent, _state(None))
    assert state["status"] == "awaiting_input"
    assert state["current_step"] == "understand"
    assert calls == ["retrieve", "understand"]


def test_stops_at_identify_from_understand():
    import porto_chatbot.workflow_runner as wr

    calls = []
    wr.identify_node.identify_subsystems = lambda a, s: (
        s.update(subsystems=["s1"], current_step="identify"),
        calls.append("identify"),
        s,
    )[2]

    state = WorkflowRunner.run_to_next_checkpoint(_agent(), _state("understand"))
    assert state["status"] == "awaiting_input"
    assert state["current_step"] == "identify"


def test_runs_to_completed_from_generate():
    import porto_chatbot.workflow_runner as wr

    calls = []
    wr.evaluate_node.evaluate = lambda a, s: (
        s.update(evaluation={"score": 100}, current_step="evaluate"),
        calls.append("evaluate"),
        s,
    )[2]

    state = WorkflowRunner.run_to_next_checkpoint(_agent(), _state("generate"))
    assert state["status"] == "completed"
    assert state["current_step"] == "evaluate"


def test_steps_and_checkpoints_constants():
    assert STEPS == ["retrieve", "understand", "identify", "generate", "evaluate"]
    assert CHECKPOINTS == {"understand", "identify", "generate"}
