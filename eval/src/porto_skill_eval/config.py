from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from smelt import LLMConfig, configure
from smelt.given.agents.llm import LLMClient


@dataclass(frozen=True)
class EvalClients:
    agent: LLMClient
    judge: LLMClient
    challenger: LLMClient | None = None
    doctor: LLMClient | None = None


def build_client(config: LLMConfig) -> LLMClient:
    """Build a role client, disabling DeepSeek reasoning that Smelt cannot replay."""
    hostname = urlparse(config.base_url or "").hostname or ""
    if config.provider == "openai" and hostname.lower().endswith("deepseek.com"):
        from smelt.given.agents.llm import OpenAIChatClient

        return OpenAIChatClient(
            config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            extra_body={"thinking": {"type": "disabled"}},
        )
    return config.build()


def load_clients() -> EvalClients:
    """Build role clients; optional roles inherit the previous evaluation role."""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    configure(env_file=str(env_file))
    configs = {}
    for role in ("agent", "judge", "challenger", "doctor"):
        config = LLMConfig.from_role(role)
        configs[role] = replace(config, provider=config.provider.lower())
    missing = []
    if not configs["agent"].model:
        missing.append("SMELT_AGENT_MODEL")
    if not configs["agent"].api_key:
        missing.append("SMELT_AGENT_API_KEY")
    if missing:
        raise ValueError(
            "Missing required agent model configuration: "
            + ", ".join(missing)
            + ". Fill these values in eval/.env."
        )

    agent = build_client(configs["agent"])

    def optional_client(role: str, fallback: LLMClient) -> LLMClient:
        config = configs[role]
        if not config.model:
            return fallback
        if not config.api_key:
            raise ValueError(
                f"SMELT_{role.upper()}_API_KEY is required when "
                f"SMELT_{role.upper()}_MODEL is set. Fill it in eval/.env."
            )
        return build_client(config)

    judge = optional_client("judge", agent)
    doctor = optional_client("doctor", judge)
    challenger = optional_client("challenger", judge) if configs["challenger"].model else None

    return EvalClients(
        agent=agent,
        judge=judge,
        challenger=challenger,
        doctor=doctor,
    )
