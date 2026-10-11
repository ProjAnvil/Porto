from pathlib import Path

from porto_chatbot.agent_sdk.skills import SKILLS
from porto_skill_eval.skills import export_skills


EXPECTED_SKILLS = {
    "prd-analysis",
    "subsystem-decomposition",
    "spec-generation",
    "spec-evaluation",
    "porto-memory",
}


def test_export_uses_current_porto_skill_definitions(tmp_path: Path):
    exported = export_skills(tmp_path / "export")

    assert set(exported) == EXPECTED_SKILLS == set(SKILLS)
    for name, skill_dir in exported.items():
        document = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        definition = SKILLS[name]
        assert document == (
            f"---\nname: {name}\ndescription: {definition.description}\n---\n\n"
            f"{definition.body}\n"
        )
