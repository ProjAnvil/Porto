from pathlib import Path

from porto_chatbot.agent_sdk.skills import SKILLS, deploy_skills


def export_skills(destination: Path) -> dict[str, Path]:
    """Export the production skill definitions to a Smelt-discoverable tree."""
    deploy_skills(destination)
    root = destination / ".claude" / "skills"
    return {name: root / name for name in SKILLS}
