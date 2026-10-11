from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from smelt import ScriptedLLM, SkillEvaluation, evaluate_skill
from smelt.challenge.doctor import DoctorReport, doctor as smelt_doctor

from porto_skill_eval.cases import build_cases
from porto_skill_eval.config import EvalClients
from porto_skill_eval.skills import export_skills
from porto_skill_eval.tools import build_tools, tools_for_skill


@dataclass(frozen=True)
class PreflightResult:
    evaluations: tuple[SkillEvaluation, ...]
    case_counts: dict[str, int]

    @property
    def skills(self) -> tuple[str, ...]:
        return tuple(evaluation.skill_name for evaluation in self.evaluations)


def preflight() -> PreflightResult:
    """Export current skills and run static lint without network/model config."""
    with TemporaryDirectory(prefix="porto-smelt-preflight-") as directory:
        exported = export_skills(Path(directory))
        cases = build_cases(ScriptedLLM(["unused: judge is not invoked in preflight"]))
        if set(exported) != set(cases):
            raise ValueError("Exported skill inventory does not match the behavior-case inventory")
        if len(build_tools()) != 6:
            raise ValueError("Fixture tool inventory changed; review tool safety before evaluation")

        evaluations = tuple(
            evaluate_skill(skill_path)
            .with_writing(enabled=False)
            .with_suggestions(enabled=False)
            .with_challenge(enabled=False)
            .run()
            for skill_path in exported.values()
        )
        return PreflightResult(
            evaluations=evaluations,
            case_counts={name: len(skill_cases) for name, skill_cases in cases.items()},
        )


def evaluate(
    skill_name: str | None,
    output_dir: Path,
    *,
    clients: EvalClients,
    challenge: bool = True,
) -> list[SkillEvaluation]:
    """Run Smelt behavior, writing, lint, and optional challenge for selected skills."""
    with TemporaryDirectory(prefix="porto-smelt-eval-") as directory:
        exported = export_skills(Path(directory))
        if skill_name is not None and skill_name not in exported:
            raise ValueError(f"Unknown skill {skill_name!r}; choose one of: {', '.join(exported)}")
        selected = [skill_name] if skill_name else list(exported)
        cases = build_cases(clients.judge)
        results = []
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        for name in selected:
            result = (
                evaluate_skill(
                    exported[name],
                    judge=clients.judge,
                    agent_llm=clients.agent,
                    challenger=clients.challenger or clients.judge,
                    tools=tools_for_skill(name),
                )
                .with_cases(*cases[name])
                .with_times(3)
                .with_challenge(enabled=challenge)
                .run()
            )
            result.save(output_dir / f"{name}.md")
            result.save(output_dir / f"{name}.json")
            results.append(result)
        return results


def run_doctors(
    skill_name: str | None,
    output_dir: Path,
    *,
    clients: EvalClients,
    min_score: float = 0.8,
) -> list[DoctorReport]:
    """Run Smelt's mutation doctor and judge canary for selected skills."""
    from porto_skill_eval.doctor_cases import configure_judge

    with TemporaryDirectory(prefix="porto-smelt-doctor-") as directory:
        root = Path(directory)
        exported = export_skills(root)
        if skill_name is not None and skill_name not in exported:
            raise ValueError(f"Unknown skill {skill_name!r}; choose one of: {', '.join(exported)}")
        selected = [skill_name] if skill_name else list(exported)
        configure_judge(clients.judge)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        reports = []
        try:
            for name in selected:
                case_file = Path(__file__).resolve().parents[2] / "cases" / f"{name}.py"
                report = smelt_doctor(
                    [case_file],
                    skill=exported[name],
                    doctor=clients.doctor or clients.judge,
                    min_score=min_score,
                    tools=tools_for_skill(name),
                )
                report.save(output_dir / f"{name}.doctor.md")
                report.save(output_dir / f"{name}.doctor.json")
                reports.append(report)
        finally:
            configure_judge(None)
        return reports
