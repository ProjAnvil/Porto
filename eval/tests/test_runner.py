from pathlib import Path

import pytest
from smelt import ScriptedLLM

from porto_skill_eval.config import EvalClients, load_clients
from porto_skill_eval.cases import build_cases
from porto_skill_eval.runner import evaluate, preflight, run_doctors


def test_preflight_lints_all_skills_without_model_configuration(monkeypatch):
    for key in (
        "SMELT_AGENT_MODEL",
        "SMELT_AGENT_API_KEY",
        "SMELT_JUDGE_MODEL",
        "SMELT_JUDGE_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    result = preflight()

    assert set(result.skills) == {
        "prd-analysis",
        "subsystem-decomposition",
        "spec-generation",
        "spec-evaluation",
        "porto-memory",
    }
    expected_cases = build_cases(ScriptedLLM(["unused"]))
    assert result.case_counts == {name: len(items) for name, items in expected_cases.items()}
    assert all(skill.lint_report is not None for skill in result.evaluations)


def test_full_evaluation_reports_missing_model_names_without_secrets(monkeypatch):
    monkeypatch.setenv("SMELT_AGENT_API_KEY", "do-not-print-this")
    monkeypatch.setenv("SMELT_AGENT_MODEL", "")

    with pytest.raises(ValueError, match="SMELT_AGENT_MODEL") as exc:
        load_clients()

    assert "do-not-print-this" not in str(exc.value)


def test_evaluate_writes_markdown_and_json_with_injected_offline_clients(tmp_path: Path):
    clients = EvalClients(
        agent=ScriptedLLM(["支付授权和退款是 payment-service 的职责。"]),
        judge=ScriptedLLM(['{"score": 1, "reason": "满足评估标准。"}']),
        challenger=None,
    )

    results = evaluate("spec-evaluation", tmp_path, clients=clients, challenge=False)

    assert len(results) == 1
    assert (tmp_path / "spec-evaluation.md").is_file()
    assert (tmp_path / "spec-evaluation.json").is_file()


def test_doctor_writes_markdown_and_json_and_loads_case_inventory(tmp_path: Path, monkeypatch):
    from smelt.challenge.doctor import DoctorReport
    import porto_skill_eval.runner as runner

    clients = EvalClients(
        agent=ScriptedLLM(["agent"]),
        judge=ScriptedLLM(["judge"]),
        challenger=None,
        doctor=ScriptedLLM(["doctor"]),
    )
    seen = {}

    def fake_doctor(case_files, *, skill, doctor, min_score, tools):
        seen["case_files"] = list(case_files)
        seen["skill"] = skill
        seen["doctor"] = doctor
        seen["tools"] = tools
        return DoctorReport(skill_path=str(skill), cases_tested=2, mutation_score=1.0, min_score=min_score)

    monkeypatch.setattr(runner, "smelt_doctor", fake_doctor)

    results = run_doctors("porto-memory", tmp_path, clients=clients)

    assert len(results) == 1
    assert len(seen["case_files"]) == 1
    assert seen["skill"].name == "porto-memory"
    assert seen["doctor"] is clients.doctor
    assert (tmp_path / "porto-memory.doctor.md").is_file()
    assert (tmp_path / "porto-memory.doctor.json").is_file()


def test_smelt_doctor_case_modules_load_all_behavior_cases():
    from smelt.challenge.doctor import load_cases_and_guards
    from porto_skill_eval.doctor_cases import configure_judge

    configure_judge(ScriptedLLM(["unused during doctor case loading"]))
    try:
        case_root = Path(__file__).resolve().parents[1] / "cases"
        for skill_name in (
            "prd-analysis",
            "subsystem-decomposition",
            "spec-generation",
            "spec-evaluation",
            "porto-memory",
        ):
            cases, _ = load_cases_and_guards(case_root / f"{skill_name}.py")
            assert len(cases) == len(build_cases(ScriptedLLM(["unused"]))[skill_name])
    finally:
        configure_judge(None)
