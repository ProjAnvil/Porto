"""Test that deploy_skills creates correct SKILL.md files from code templates."""
import tempfile
from pathlib import Path

from porto_chatbot.agent_sdk.skills import deploy_skills, SKILLS, CLAUDE_MD


def test_skills_dict_has_required_entries():
    assert "prd-analysis" in SKILLS
    assert "subsystem-decomposition" in SKILLS
    assert "spec-generation" in SKILLS
    assert "spec-evaluation" in SKILLS
    assert "porto-memory" in SKILLS


def test_deploy_skills_creates_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        deploy_skills(data_dir)

        # CLAUDE.md exists
        assert (data_dir / ".claude" / "CLAUDE.md").exists()

        # Each skill has SKILL.md
        for name in SKILLS:
            skill_file = data_dir / ".claude" / "skills" / name / "SKILL.md"
            assert skill_file.exists(), f"Missing skill: {name}"
            content = skill_file.read_text(encoding="utf-8")
            assert content.startswith("---")  # YAML frontmatter
            assert f"name: {name}" in content


def test_deploy_skills_is_idempotent():
    """Running twice produces same output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        deploy_skills(data_dir)
        first = (data_dir / ".claude" / "skills" / "prd-analysis" / "SKILL.md").read_text()
        deploy_skills(data_dir)
        second = (data_dir / ".claude" / "skills" / "prd-analysis" / "SKILL.md").read_text()
        assert first == second
