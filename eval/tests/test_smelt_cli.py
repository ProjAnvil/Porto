from pathlib import Path
import subprocess
import tempfile

from porto_skill_eval.skills import export_skills


def test_smelt_validate_discovers_all_exported_skills():
    with tempfile.TemporaryDirectory(prefix="porto-smelt-cli-test-") as temp_dir:
        skill_root = Path(temp_dir) / ".claude" / "skills"
        export_skills(Path(temp_dir))
        completed = subprocess.run(
            [
                "uv",
                "run",
                "smelt",
                "validate",
                str(skill_root),
                "--format",
                "markdown",
                "--fail-under",
                "0",
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=False,
        )

        # Smelt exits 1 for F-grade skills even when --fail-under is 0.
        output = completed.stdout + completed.stderr
        for skill_name in (
            "prd-analysis",
            "subsystem-decomposition",
            "spec-generation",
            "spec-evaluation",
            "porto-memory",
        ):
            assert skill_name in output
        assert "Score" in output or "/" in output
