from porto_skill_eval.cli import main


def test_evaluate_rejects_unknown_skill_before_loading_clients(capsys):
    exit_code = main(["evaluate", "--skill", "typo"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code != 0
    assert "Unknown skill" in output
    assert "typo" in output


def test_full_eval_missing_models_has_actionable_message(capsys, monkeypatch):
    for key in (
        "SMELT_AGENT_MODEL",
        "SMELT_AGENT_API_KEY",
    ):
        monkeypatch.setenv(key, "")

    exit_code = main(["evaluate", "--skill", "prd-analysis"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert exit_code != 0
    assert "SMELT_AGENT_MODEL" in output
    assert "sk-" not in output


def test_preflight_prints_lint_scores_for_all_skills(capsys):
    exit_code = main(["preflight"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Preflight discovered 5 Porto skills." in output
    skill_lines = [line for line in output.splitlines() if line.startswith("- ")]
    assert len(skill_lines) == 5
    assert all("lint " in line and "/100 (" in line for line in skill_lines)


def test_full_command_runs_challenge_evaluation_and_doctor(monkeypatch, tmp_path):
    import porto_skill_eval.cli as cli
    from porto_skill_eval.config import EvalClients
    from types import SimpleNamespace

    calls = []
    clients = EvalClients(agent=object(), judge=object(), doctor=object())
    monkeypatch.setattr(cli, "load_clients", lambda: clients)
    result = SimpleNamespace(
        skill_name="prd-analysis",
        overall_score=90,
        to_dict=lambda: {"challenge": {"score": 0.9}},
    )
    monkeypatch.setattr(cli, "evaluate", lambda skill, output, **kwargs: calls.append(("evaluate", kwargs)) or [result])
    monkeypatch.setattr(cli, "run_doctors", lambda skill, output, **kwargs: calls.append(("doctor", kwargs)) or [])

    exit_code = cli.main(["full", "--output", str(tmp_path)])

    assert exit_code == 0
    assert [name for name, _ in calls] == ["evaluate", "doctor"]
    assert calls[0][1]["challenge"] is True
    assert calls[1][1]["min_score"] == 0.2


def test_full_command_allows_stricter_doctor_gate(monkeypatch, tmp_path):
    import porto_skill_eval.cli as cli
    from porto_skill_eval.config import EvalClients

    calls = []
    monkeypatch.setattr(cli, "load_clients", lambda: EvalClients(agent=object(), judge=object()))
    monkeypatch.setattr(cli, "evaluate", lambda skill, output, **kwargs: [])
    monkeypatch.setattr(cli, "run_doctors", lambda skill, output, **kwargs: calls.append(kwargs) or [])

    assert cli.main(["full", "--output", str(tmp_path), "--min-doctor-score", "0.65"]) == 0
    assert calls[0]["min_score"] == 0.65


def test_full_command_fails_when_challenge_score_is_below_gate(monkeypatch, tmp_path):
    import porto_skill_eval.cli as cli
    from porto_skill_eval.config import EvalClients
    from types import SimpleNamespace

    result = SimpleNamespace(
        skill_name="prd-analysis",
        overall_score=90,
        to_dict=lambda: {"challenge": {"score": 0.79}},
    )
    monkeypatch.setattr(cli, "load_clients", lambda: EvalClients(agent=object(), judge=object()))
    monkeypatch.setattr(cli, "evaluate", lambda skill, output, **kwargs: [result])
    monkeypatch.setattr(cli, "run_doctors", lambda skill, output, **kwargs: [])

    assert cli.main(["full", "--output", str(tmp_path)]) == 1
