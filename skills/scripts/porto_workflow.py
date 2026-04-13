#!/usr/bin/env python3
"""
Porto Workflow Manager - Persistent state machine for PRD decomposition workflow.

Solves the fundamental limitation of pure-prompt execution: the LLM loses track of
workflow state as context grows. This script acts as a persistent state machine
stored on disk, so the orchestrating agent never relies on in-context memory.

Usage:
  python porto_workflow.py init           --name <id> --inputs <f1,f2,...> [--project <name>]
  python porto_workflow.py current        --workflow <id>
  python porto_workflow.py step-start     --workflow <id> --step <n>
  python porto_workflow.py step-complete  --workflow <id> --step <n> [--output <file>] [--summary <json>]
  python porto_workflow.py step-fail      --workflow <id> --step <n> [--error <msg>]
  python porto_workflow.py advance        --workflow <id>
  python porto_workflow.py status         [--workflow <id>] [--full]
  python porto_workflow.py list           [--all] [--recent <days>] [--status <s>] [--name <kw>]
  python porto_workflow.py resume         [--workflow <id>]
  python porto_workflow.py set-subsystems --workflow <id> --subsystems <json>
  python porto_workflow.py cleanup        [--older-than <days>]

State file: ~/.porto/workflows/{workflow_id}/workflow.json
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Porto home and workflows dir – configurable via PORTO_HOME env var or
# the --porto-home CLI flag.  Defaults to ~/.porto.
_porto_home: Path | None = None


def _get_porto_home() -> Path:
    global _porto_home
    if _porto_home is not None:
        return _porto_home
    return Path(os.environ.get("PORTO_HOME", Path.home() / ".porto"))


def _set_porto_home(path: Path) -> None:
    global _porto_home
    _porto_home = path


def _get_workflows_dir() -> Path:
    return _get_porto_home() / "workflows"

STEP_DEFINITIONS = {
    1: {
        "name": "understanding",
        "description": "Business requirements understanding",
        "output_file": "md/step1_understanding.md",
        "skill": "prd-decomposition",
    },
    2: {
        "name": "subsystem_identification",
        "description": "Subsystem identification",
        "output_file": "md/step2_subsystems.md",
        "skill": "subsystem-identification",
    },
    3: {
        "name": "subsystem_context_generation",
        "description": "Subsystem context generation (Mermaid diagrams)",
        "output_file": "md/step3_context.md",
        "skill": "subsystem-context-generation",
    },
    4: {
        "name": "subsystem_specification",
        "description": "Subsystem specification generation",
        "output_dir": "md/step4",
        "skill": "subsystem-specification",
    },
}

TOTAL_STEPS = len(STEP_DEFINITIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _die(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(code)


def workflow_dir(workflow_id: str) -> Path:
    return _get_workflows_dir() / workflow_id


def workflow_path(workflow_id: str) -> Path:
    return workflow_dir(workflow_id) / "workflow.json"


def load_workflow(workflow_id: str) -> dict:
    path = workflow_path(workflow_id)
    if not path.exists():
        _die(f"Workflow not found: {workflow_id}\n  Expected: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_workflow(state: dict) -> None:
    wf_id = state["workflow_id"]
    wf_dir = workflow_dir(wf_id)
    wf_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    with open(workflow_path(wf_id), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    # Sync current_step file for backward compatibility
    step_file = wf_dir / "current_step"
    step_file.write_text(str(state["current_step"]))


def resolve_workflow_id(identifier: str) -> Optional[str]:
    """Resolve a short ID (>=8 chars) or full UUID to a workflow_id."""
    if not _get_workflows_dir().exists():
        return None

    # Exact match
    if workflow_path(identifier).exists():
        return identifier

    # Short ID match (>= 8 chars)
    if len(identifier) >= 8:
        matches = [
            d.name
            for d in _get_workflows_dir().iterdir()
            if d.is_dir() and d.name.startswith(identifier)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            _die(
                f"Ambiguous workflow ID: '{identifier}'. Matches: {', '.join(matches)}"
            )
    return None


def find_active_workflow() -> Optional[dict]:
    """Find the most recent in-progress or paused workflow."""
    if not _get_workflows_dir().exists():
        return None

    candidates = []
    for d in _get_workflows_dir().iterdir():
        if not d.is_dir():
            continue
        wf_file = d / "workflow.json"
        if not wf_file.exists():
            continue
        try:
            with open(wf_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("status") in ("in_progress", "paused"):
                candidates.append(state)
        except (json.JSONDecodeError, OSError):
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return candidates[0]


def _make_step(step_num: int) -> dict:
    defn = STEP_DEFINITIONS[step_num]
    return {
        "name": defn["name"],
        "description": defn["description"],
        "status": "pending",
        "output": defn.get("output_file") or defn.get("output_dir"),
        "started_at": None,
        "completed_at": None,
        "summary": {},
    }


def _step_progress(state: dict) -> dict:
    """Compute step progress summary."""
    completed = 0
    failed = 0
    for i in range(1, TOTAL_STEPS + 1):
        step = state["steps"].get(str(i), {})
        if step.get("status") == "completed":
            completed += 1
        elif step.get("status") == "failed":
            failed += 1
    return {
        "total": TOTAL_STEPS,
        "completed": completed,
        "failed": failed,
        "pending": TOTAL_STEPS - completed - failed,
        "progress_pct": round(completed / TOTAL_STEPS * 100),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args):
    """Create a new workflow with UUID, copy inputs, initialize state."""
    input_paths = [Path(p).expanduser().resolve() for p in args.inputs.split(",")]

    # Validate inputs
    missing = [str(p) for p in input_paths if not p.exists()]
    if missing:
        _die(f"Input files not found: {', '.join(missing)}")

    # Generate workflow ID
    wf_id = str(uuid.uuid4())
    wf_dir = workflow_dir(wf_id)
    wf_dir.mkdir(parents=True, exist_ok=True)

    # Copy input files
    inputs_dir = wf_dir / "inputs"
    inputs_dir.mkdir(exist_ok=True)
    # Create md/ directory for markdown outputs
    md_dir = wf_dir / "md"
    md_dir.mkdir(exist_ok=True)
    input_records = []
    for p in input_paths:
        dest = inputs_dir / p.name
        shutil.copy2(str(p), str(dest))
        input_records.append(
            {
                "original_path": str(p),
                "name": p.name,
                "copied_to": f"inputs/{p.name}",
            }
        )

    # Project name
    project_name = args.project or input_paths[0].stem

    # Initialize state
    steps = {str(i): _make_step(i) for i in range(1, TOTAL_STEPS + 1)}
    # Mark step 1 as in_progress
    steps["1"]["status"] = "in_progress"
    steps["1"]["started_at"] = _now()

    state = {
        "workflow_id": wf_id,
        "project_name": project_name,
        "created_at": _now(),
        "updated_at": _now(),
        "status": "in_progress",
        "current_step": 1,
        "input_files": input_records,
        "steps": steps,
        "subsystems": [],
        "completed_at": None,
    }

    save_workflow(state)

    _ok(
        {
            "status": "initialized",
            "workflow_id": wf_id,
            "project_name": project_name,
            "workspace": str(wf_dir),
            "input_files": [r["name"] for r in input_records],
            "current_step": 1,
            "next_action": "Execute Step 1 skill: prd-decomposition",
            "step_info": {
                "name": STEP_DEFINITIONS[1]["name"],
                "description": STEP_DEFINITIONS[1]["description"],
                "output_file": STEP_DEFINITIONS[1]["output_file"],
            },
        }
    )


def cmd_current(args):
    """Show current step info for a workflow."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)
    step_num = state["current_step"]
    step = state["steps"].get(str(step_num), {})
    defn = STEP_DEFINITIONS.get(step_num, {})

    _ok(
        {
            "workflow_id": state["workflow_id"],
            "project_name": state["project_name"],
            "status": state["status"],
            "current_step": step_num,
            "step": {
                "name": step.get("name", defn.get("name")),
                "status": step.get("status"),
                "output": step.get("output"),
                "started_at": step.get("started_at"),
                "skill": defn.get("skill"),
            },
            "workspace": str(workflow_dir(resolved)),
        }
    )


def cmd_step_start(args):
    """Mark a step as in_progress."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)
    step_num = str(args.step)

    if step_num not in state["steps"]:
        _die(f"Invalid step: {args.step}. Valid steps: 1-{TOTAL_STEPS}")

    step = state["steps"][step_num]

    if step["status"] == "completed":
        _die(f"Step {args.step} is already completed")

    step["status"] = "in_progress"
    step["started_at"] = _now()
    state["current_step"] = args.step
    state["status"] = "in_progress"

    save_workflow(state)
    _ok(
        {
            "status": "step_started",
            "workflow_id": state["workflow_id"],
            "step": args.step,
            "step_name": step["name"],
            "skill": STEP_DEFINITIONS[args.step].get("skill"),
        }
    )


def cmd_step_complete(args):
    """Mark a step as completed."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)
    step_num = str(args.step)

    if step_num not in state["steps"]:
        _die(f"Invalid step: {args.step}. Valid steps: 1-{TOTAL_STEPS}")

    step = state["steps"][step_num]
    step["status"] = "completed"
    step["completed_at"] = _now()

    if args.output:
        step["output"] = args.output

    if args.summary:
        try:
            step["summary"] = json.loads(args.summary)
        except json.JSONDecodeError:
            _die(f"Invalid JSON in --summary: {args.summary}")

    # Collect subsystems from step 2 summary
    if args.step == 2 and args.summary:
        try:
            summary = json.loads(args.summary)
            if "subsystems" in summary:
                state["subsystems"] = summary["subsystems"]
        except json.JSONDecodeError:
            pass

    # Check if all steps completed
    all_done = all(
        state["steps"][str(i)]["status"] == "completed"
        for i in range(1, TOTAL_STEPS + 1)
    )
    if all_done:
        state["status"] = "completed"
        state["completed_at"] = _now()

    save_workflow(state)

    result = {
        "status": "step_completed",
        "workflow_id": state["workflow_id"],
        "step": args.step,
        "step_name": step["name"],
        "output": step["output"],
        **_step_progress(state),
    }

    if all_done:
        result["workflow_completed"] = True

    _ok(result)


def cmd_step_fail(args):
    """Mark a step as failed."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)
    step_num = str(args.step)

    if step_num not in state["steps"]:
        _die(f"Invalid step: {args.step}. Valid steps: 1-{TOTAL_STEPS}")

    step = state["steps"][step_num]
    step["status"] = "failed"
    step["completed_at"] = _now()
    step["error"] = args.error or "unknown error"
    state["status"] = "failed"

    save_workflow(state)
    _ok(
        {
            "status": "step_failed",
            "workflow_id": state["workflow_id"],
            "step": args.step,
            "step_name": step["name"],
            "error": step["error"],
            **_step_progress(state),
        }
    )


def cmd_advance(args):
    """Move to the next step after user review."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)
    current = state["current_step"]

    if current >= TOTAL_STEPS:
        # Check if already all done
        all_done = all(
            state["steps"][str(i)]["status"] == "completed"
            for i in range(1, TOTAL_STEPS + 1)
        )
        if all_done:
            _ok(
                {
                    "status": "workflow_already_completed",
                    "workflow_id": state["workflow_id"],
                    "next_action": "All steps completed. Review outputs.",
                }
            )
            return
        _die(
            f"Cannot advance: current step is {current} (max {TOTAL_STEPS}). "
            f"Mark step {current} as completed first."
        )

    # Verify current step is completed
    current_step = state["steps"][str(current)]
    if current_step["status"] != "completed":
        _die(
            f"Cannot advance: Step {current} status is '{current_step['status']}', not 'completed'. "
            f"Complete step {current} first."
        )

    next_step = current + 1
    state["current_step"] = next_step
    state["status"] = "paused"  # Paused = waiting for user to run continue

    next_step_data = state["steps"][str(next_step)]
    next_defn = STEP_DEFINITIONS[next_step]

    # Check prerequisite files
    wf_dir_path = workflow_dir(resolved)
    prerequisites = []
    for i in range(1, next_step):
        prev = state["steps"][str(i)]
        if prev.get("output") and prev["status"] == "completed":
            prereq_file = wf_dir_path / prev["output"]
            prerequisites.append(
                {
                    "step": i,
                    "file": prev["output"],
                    "exists": prereq_file.exists(),
                }
            )

    save_workflow(state)
    _ok(
        {
            "status": "advanced",
            "workflow_id": state["workflow_id"],
            "from_step": current,
            "to_step": next_step,
            "step": {
                "name": next_defn["name"],
                "description": next_defn["description"],
                "skill": next_defn["skill"],
                "output": next_defn.get("output_file") or next_defn.get("output_dir"),
            },
            "prerequisites": prerequisites,
            "next_action": f"Execute Step {next_step} skill: {next_defn['skill']}",
        }
    )


def cmd_status(args):
    """Display workflow status."""
    wf_id = args.workflow

    if not wf_id:
        # Find active workflow
        active = find_active_workflow()
        if not active:
            _ok(
                {
                    "status": "no_active_workflow",
                    "message": "No active workflow found",
                    "suggestion": "Start a new workflow with /porto.gen or list all with /porto.list",
                }
            )
            return
        state = active
    else:
        resolved = resolve_workflow_id(wf_id)
        if not resolved:
            _die(f"Workflow not found: {wf_id}")
        state = load_workflow(resolved)

    wf_dir_path = workflow_dir(state["workflow_id"])

    # Build step details
    steps_detail = []
    for i in range(1, TOTAL_STEPS + 1):
        step = state["steps"].get(str(i), {})
        defn = STEP_DEFINITIONS.get(i, {})
        output_file = step.get("output") or defn.get("output_file") or defn.get(
            "output_dir"
        )
        output_path = wf_dir_path / output_file if output_file else None
        steps_detail.append(
            {
                "step": i,
                "name": step.get("name", defn.get("name")),
                "status": step.get("status", "pending"),
                "output": output_file,
                "output_exists": output_path.exists() if output_path else False,
                "output_size": (
                    output_path.stat().st_size if output_path and output_path.exists() else None
                ),
                "started_at": step.get("started_at"),
                "completed_at": step.get("completed_at"),
                "summary": step.get("summary", {}),
            }
        )

    result = {
        "workflow_id": state["workflow_id"],
        "project_name": state["project_name"],
        "status": state["status"],
        "current_step": state["current_step"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "completed_at": state.get("completed_at"),
        "workspace": str(wf_dir_path),
        "input_files": [f["name"] for f in state.get("input_files", [])],
        "steps": steps_detail,
        "subsystems": state.get("subsystems", []),
        **_step_progress(state),
    }

    # Full preview mode
    if args.full:
        previews = {}
        for sd in steps_detail:
            if sd["output_exists"] and sd["output"]:
                fpath = wf_dir_path / sd["output"]
                if fpath.is_file():
                    try:
                        content = fpath.read_text(encoding="utf-8")
                        lines = content.splitlines()
                        previews[f"step{sd['step']}"] = (
                            "\n".join(lines[:50])
                            if len(lines) > 50
                            else content
                        )
                    except OSError:
                        pass
        result["previews"] = previews

    _ok(result)


def cmd_list(args):
    """List workflows with optional filters."""
    if not _get_workflows_dir().exists():
        _ok({"workflows": [], "total": 0})
        return

    workflows = []
    for d in sorted(_get_workflows_dir().iterdir(), reverse=True):
        if not d.is_dir():
            continue
        wf_file = d / "workflow.json"
        if not wf_file.exists():
            continue
        try:
            with open(wf_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            workflows.append(state)
        except (json.JSONDecodeError, OSError):
            continue

    # Apply filters
    if args.status:
        workflows = [w for w in workflows if w.get("status") == args.status]

    if args.name:
        kw = args.name.lower()
        workflows = [
            w for w in workflows if kw in w.get("project_name", "").lower()
        ]

    if args.step is not None:
        workflows = [
            w for w in workflows if w.get("current_step") == args.step
        ]

    if not args.all:
        days = args.recent or 3
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for w in workflows:
            updated = w.get("updated_at") or w.get("created_at")
            if updated:
                try:
                    dt = datetime.fromisoformat(updated)
                    if dt >= cutoff:
                        filtered.append(w)
                except ValueError:
                    filtered.append(w)  # Include if date parsing fails
            else:
                filtered.append(w)
        workflows = filtered

    # Build summary
    result_list = []
    for w in workflows:
        result_list.append(
            {
                "workflow_id": w["workflow_id"],
                "short_id": w["workflow_id"][:8],
                "project_name": w.get("project_name", ""),
                "current_step": w.get("current_step"),
                "status": w.get("status"),
                "created_at": w.get("created_at"),
                "updated_at": w.get("updated_at"),
                "completed_at": w.get("completed_at"),
            }
        )

    # Stats
    statuses = [w.get("status", "") for w in workflows]
    _ok(
        {
            "total": len(result_list),
            "completed": statuses.count("completed"),
            "in_progress": statuses.count("in_progress"),
            "paused": statuses.count("paused"),
            "failed": statuses.count("failed"),
            "workflows": result_list,
        }
    )


def cmd_resume(args):
    """Show resumable workflows or resume a specific one."""
    wf_id = args.workflow

    if wf_id:
        resolved = resolve_workflow_id(wf_id)
        if not resolved:
            _die(f"Workflow not found: {wf_id}")
        state = load_workflow(resolved)
        wf_dir_path = workflow_dir(resolved)

        # Build resume info
        current = state["current_step"]
        current_step = state["steps"].get(str(current), {})
        defn = STEP_DEFINITIONS.get(current, {})

        # Check prerequisites
        prereqs_ok = True
        for i in range(1, current):
            prev = state["steps"].get(str(i), {})
            if prev.get("status") != "completed":
                prereqs_ok = False
                break

        _ok(
            {
                "action": "resume",
                "workflow_id": state["workflow_id"],
                "project_name": state["project_name"],
                "status": state["status"],
                "resume_from_step": current,
                "step": {
                    "name": current_step.get("name", defn.get("name")),
                    "status": current_step.get("status"),
                    "skill": defn.get("skill"),
                },
                "prerequisites_ok": prereqs_ok,
                "workspace": str(wf_dir_path),
            }
        )
    else:
        # List resumable workflows
        if not _get_workflows_dir().exists():
            _ok({"resumable": [], "total": 0})
            return

        resumable = []
        for d in sorted(_get_workflows_dir().iterdir(), reverse=True):
            if not d.is_dir():
                continue
            wf_file = d / "workflow.json"
            if not wf_file.exists():
                continue
            try:
                with open(wf_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("status") in ("in_progress", "paused", "failed"):
                    current = state.get("current_step", 1)
                    step = state["steps"].get(str(current), {})
                    resumable.append(
                        {
                            "workflow_id": state["workflow_id"],
                            "short_id": state["workflow_id"][:8],
                            "project_name": state.get("project_name", ""),
                            "status": state.get("status"),
                            "resume_from_step": current,
                            "step_name": step.get("name", ""),
                            "updated_at": state.get("updated_at"),
                        }
                    )
            except (json.JSONDecodeError, OSError):
                continue

        _ok({"resumable": resumable, "total": len(resumable)})


def cmd_set_subsystems(args):
    """Record identified subsystems (called after Step 2)."""
    wf_id = args.workflow
    resolved = resolve_workflow_id(wf_id)
    if not resolved:
        _die(f"Workflow not found: {wf_id}")

    state = load_workflow(resolved)

    try:
        subsystems = json.loads(args.subsystems)
    except json.JSONDecodeError:
        _die(f"Invalid JSON in --subsystems: {args.subsystems}")

    if not isinstance(subsystems, list):
        _die("--subsystems must be a JSON array")

    state["subsystems"] = subsystems
    save_workflow(state)

    _ok(
        {
            "status": "subsystems_set",
            "workflow_id": state["workflow_id"],
            "subsystems": [
                s.get("name", str(s)) if isinstance(s, dict) else str(s)
                for s in subsystems
            ],
            "count": len(subsystems),
        }
    )


def cmd_cleanup(args):
    """Remove workflow directories older than N days."""
    max_age_days = args.older_than or 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    removed = []

    if not _get_workflows_dir().exists():
        _ok({"removed": [], "count": 0})
        return

    for d in _get_workflows_dir().iterdir():
        if not d.is_dir():
            continue
        wf_file = d / "workflow.json"
        if not wf_file.exists():
            # Remove orphaned directories
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d))
            continue

        try:
            with open(wf_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            # Only remove completed or failed workflows
            if state.get("status") not in ("completed", "failed"):
                continue
            updated = state.get("updated_at") or state.get("completed_at") or state.get("created_at")
            if updated:
                dt = datetime.fromisoformat(updated)
                if dt < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(str(d))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    _ok({"removed": removed, "count": len(removed)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Porto Workflow Manager - persistent state for PRD decomposition"
    )
    parser.add_argument(
        "--porto-home",
        default=None,
        help="Override porto home directory (default: $PORTO_HOME or ~/.porto)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Create a new workflow")
    p_init.add_argument("--name", required=True, help="Workflow identifier")
    p_init.add_argument(
        "--inputs", required=True, help="Comma-separated input file paths"
    )
    p_init.add_argument("--project", default=None, help="Project name")

    # current
    p_cur = sub.add_parser("current", help="Show current step info")
    p_cur.add_argument("--workflow", required=True, help="Workflow ID")

    # step-start
    p_ss = sub.add_parser("step-start", help="Mark a step as in_progress")
    p_ss.add_argument("--workflow", required=True)
    p_ss.add_argument("--step", required=True, type=int)

    # step-complete
    p_sc = sub.add_parser("step-complete", help="Mark a step as completed")
    p_sc.add_argument("--workflow", required=True)
    p_sc.add_argument("--step", required=True, type=int)
    p_sc.add_argument("--output", default=None, help="Output file name")
    p_sc.add_argument("--summary", default=None, help="JSON summary of step results")

    # step-fail
    p_sf = sub.add_parser("step-fail", help="Mark a step as failed")
    p_sf.add_argument("--workflow", required=True)
    p_sf.add_argument("--step", required=True, type=int)
    p_sf.add_argument("--error", default=None, help="Error message")

    # advance
    p_adv = sub.add_parser("advance", help="Move to next step after review")
    p_adv.add_argument("--workflow", required=True)

    # status
    p_stat = sub.add_parser("status", help="Display workflow status")
    p_stat.add_argument("--workflow", default=None, help="Workflow ID (omit for active)")
    p_stat.add_argument("--full", action="store_true", help="Include output previews")

    # list
    p_list = sub.add_parser("list", help="List workflows")
    p_list.add_argument("--all", action="store_true", help="Show all workflows")
    p_list.add_argument("--recent", type=int, default=None, help="Last N days (default 3)")
    p_list.add_argument("--status", default=None, help="Filter by status")
    p_list.add_argument("--name", default=None, help="Filter by project name")
    p_list.add_argument("--step", type=int, default=None, help="Filter by step number")

    # resume
    p_res = sub.add_parser("resume", help="Resume a workflow")
    p_res.add_argument("--workflow", default=None, help="Workflow ID (omit to list resumable)")

    # set-subsystems
    p_subsys = sub.add_parser("set-subsystems", help="Record identified subsystems")
    p_subsys.add_argument("--workflow", required=True)
    p_subsys.add_argument("--subsystems", required=True, help="JSON array of subsystems")

    # cleanup
    p_clean = sub.add_parser("cleanup", help="Remove old workflows")
    p_clean.add_argument("--older-than", type=int, default=30, help="Remove older than N days")

    args = parser.parse_args()

    # Apply porto-home override
    if args.porto_home:
        _set_porto_home(Path(args.porto_home).expanduser().resolve())

    commands = {
        "init": cmd_init,
        "current": cmd_current,
        "step-start": cmd_step_start,
        "step-complete": cmd_step_complete,
        "step-fail": cmd_step_fail,
        "advance": cmd_advance,
        "status": cmd_status,
        "list": cmd_list,
        "resume": cmd_resume,
        "set-subsystems": cmd_set_subsystems,
        "cleanup": cmd_cleanup,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
