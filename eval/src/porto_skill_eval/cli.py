import argparse
from datetime import datetime
from pathlib import Path
import sys

from porto_skill_eval.config import load_clients
from porto_skill_eval.runner import evaluate, preflight, run_doctors
from porto_chatbot.agent_sdk.skills import SKILLS


SKILL_NAMES = tuple(SKILLS)
DEFAULT_REPORTS = Path(__file__).resolve().parents[2] / "reports"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="porto-skill-eval")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="export skills and run credential-free Smelt lint")
    run = commands.add_parser("evaluate", help="run Smelt behavior and writing evaluation")
    run.add_argument("--skill", metavar="NAME")
    run.add_argument("--output", type=Path)
    full = commands.add_parser("full", help="run behavior, writing, lint, challenge, and doctor")
    full.add_argument("--skill", metavar="NAME")
    full.add_argument("--output", type=Path)
    full.add_argument("--min-score", type=float, default=80.0)
    full.add_argument("--min-challenge-score", type=float, default=0.8)
    full.add_argument(
        "--min-doctor-score",
        type=float,
        default=0.2,
        help="minimum Smelt doctor mutation score (default: 0.2)",
    )
    doctor = commands.add_parser("doctor", help="run Smelt mutation doctor and judge canary")
    doctor.add_argument("--skill", metavar="NAME")
    doctor.add_argument("--output", type=Path)
    doctor.add_argument("--min-score", type=float, default=0.8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            result = preflight()
            print(f"Preflight discovered {len(result.skills)} Porto skills.")
            for evaluation in result.evaluations:
                lint = evaluation.lint_report
                grade = lint.grade.value if hasattr(lint.grade, "value") else lint.grade
                print(
                    f"- {evaluation.skill_name}: {result.case_counts[evaluation.skill_name]} cases; "
                    f"lint {lint.total_score:.1f}/100 ({grade})"
                )
            return 0

        if args.command in ("evaluate", "full", "doctor") and args.skill is not None and args.skill not in SKILL_NAMES:
            print(
                f"Unknown skill {args.skill!r}; choose one of: {', '.join(SKILL_NAMES)}",
                file=sys.stderr,
            )
            return 2
        clients = load_clients()
        if clients.judge is clients.agent:
            print("Judge profile is unset; using the configured agent profile for judge calls.")
        if clients.doctor is clients.judge and args.command in ("doctor", "full"):
            print("Doctor profile is unset; using the configured judge profile for doctor calls.")
        if clients.challenger is None and args.command in ("evaluate", "full"):
            print("Challenger profile is unset; challenge probes will use the judge profile.")

        output = args.output or DEFAULT_REPORTS / datetime.now().strftime("%Y%m%d-%H%M%S")
        if args.command == "doctor":
            reports = run_doctors(args.skill, output, clients=clients, min_score=args.min_score)
            for report in reports:
                score = "N/A" if report.mutation_score is None else f"{report.mutation_score:.0%}"
                print(f"- {Path(report.skill_path).name}: doctor mutation {score}; ok={report.ok}")
            print(f"Saved {len(reports)} Smelt doctor report(s) to {output.resolve()}")
            return 0 if all(report.ok for report in reports) else 1

        results = evaluate(
            args.skill,
            output,
            clients=clients,
            challenge=args.command in ("evaluate", "full"),
        )
        print(f"Saved {len(results)} Smelt evaluation(s) to {output.resolve()}")
        if args.command == "full":
            reports = run_doctors(
                args.skill,
                output,
                clients=clients,
                min_score=args.min_doctor_score,
            )
            score_passed = True
            challenge_passed = True
            for result in results:
                score = result.overall_score
                passed = score is not None and score >= args.min_score
                score_passed &= passed
                formatted = "N/A" if score is None else f"{score:.1f}"
                print(f"- {result.skill_name}: overall {formatted}/100; min {args.min_score:g}; passed={passed}")
                challenge = result.to_dict().get("challenge") or {}
                challenge_score = challenge.get("score")
                challenge_formatted = "N/A" if challenge_score is None else f"{challenge_score:.3f}"
                passed_challenge = (
                    challenge_score is not None and challenge_score >= args.min_challenge_score
                )
                challenge_passed &= passed_challenge
                print(
                    f"- {result.skill_name}: challenge {challenge_formatted}; "
                    f"min {args.min_challenge_score:g}; passed={passed_challenge}"
                )
            doctor_passed = all(report.ok for report in reports)
            for report in reports:
                score = "N/A" if report.mutation_score is None else f"{report.mutation_score:.0%}"
                print(f"- {Path(report.skill_path).name}: doctor mutation {score}; ok={report.ok}")
            print(f"Saved {len(reports)} Smelt doctor report(s) to {output.resolve()}")
            return 0 if score_passed and challenge_passed and doctor_passed else 1
        return 0
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - avoid leaking provider errors or request headers
        print(f"Error: evaluation failed ({type(error).__name__}); check model configuration.", file=sys.stderr)
        return 2
