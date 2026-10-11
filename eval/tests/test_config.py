from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "eval"


def _read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid env line: {line}"
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def test_local_env_matches_template_keys():
    template = _read_env(EVAL / ".env.template")
    local = _read_env(EVAL / ".env")

    assert set(template) == {
        "SMELT_AGENT_PROVIDER",
        "SMELT_AGENT_MODEL",
        "SMELT_AGENT_BASE_URL",
        "SMELT_AGENT_API_KEY",
        "SMELT_JUDGE_PROVIDER",
        "SMELT_JUDGE_MODEL",
        "SMELT_JUDGE_BASE_URL",
        "SMELT_JUDGE_API_KEY",
        "SMELT_CHALLENGER_PROVIDER",
        "SMELT_CHALLENGER_MODEL",
        "SMELT_CHALLENGER_BASE_URL",
        "SMELT_CHALLENGER_API_KEY",
        "SMELT_DOCTOR_PROVIDER",
        "SMELT_DOCTOR_MODEL",
        "SMELT_DOCTOR_BASE_URL",
        "SMELT_DOCTOR_API_KEY",
    }
    assert set(local) == set(template)
    assert all(isinstance(value, str) for value in local.values())


def test_git_ignores_local_env_and_tracks_env_template():
    ignored_env = subprocess.run(
        ["git", "check-ignore", "-q", "eval/.env"], cwd=ROOT, check=False
    )
    ignored_template = subprocess.run(
        ["git", "check-ignore", "-q", "eval/.env.template"], cwd=ROOT, check=False
    )

    assert ignored_env.returncode == 0
    assert ignored_template.returncode == 1


def test_missing_optional_roles_reuse_the_agent_profile(monkeypatch):
    from porto_skill_eval.config import load_clients

    monkeypatch.setenv("SMELT_AGENT_PROVIDER", "OpenAI")
    monkeypatch.setenv("SMELT_AGENT_MODEL", "offline-test-model")
    monkeypatch.setenv("SMELT_AGENT_BASE_URL", "http://127.0.0.1/v1")
    monkeypatch.setenv("SMELT_AGENT_API_KEY", "test-key")
    for role in ("JUDGE", "DOCTOR", "CHALLENGER"):
        for field in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
            monkeypatch.setenv(f"SMELT_{role}_{field}", "")

    clients = load_clients()

    assert clients.judge is clients.agent
    assert clients.doctor is clients.agent
    assert clients.challenger is None


def test_deepseek_compatible_client_disables_reasoning_for_tool_history(monkeypatch):
    import smelt.given.agents.llm as llm_module
    from smelt import LLMConfig
    from porto_skill_eval.config import build_client

    captured = {}

    def fake_client(model, **kwargs):
        captured.update(model=model, **kwargs)
        return object()

    monkeypatch.setattr(llm_module, "OpenAIChatClient", fake_client)
    config = LLMConfig(
        provider="openai",
        model="offline-probe-model",
        api_key="not-printed",
        base_url="https://api.deepseek.com",
    )

    build_client(config)

    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
