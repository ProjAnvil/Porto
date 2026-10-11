from smelt.given.agents.llm import LLMClient

from porto_skill_eval.cases import build_cases


_judge: LLMClient | None = None


def configure_judge(judge: LLMClient | None) -> None:
    global _judge
    _judge = judge


def cases_for(skill_name: str):
    if _judge is None:
        raise RuntimeError("doctor case loader requires a configured judge")
    return list(build_cases(_judge)[skill_name])
