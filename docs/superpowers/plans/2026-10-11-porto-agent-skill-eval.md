# Porto Agent Skill Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Build a standalone `eval/` uv project that evaluates Porto's five current Agent SDK skills with Smelt behavior cases, writing review, static lint, and local reports.

**Architecture:** The uv project pins Smelt to the approved Git commit and consumes Porto's local backend package so it can call the production `deploy_skills` function. A small Python CLI exports current skills to a temporary directory, creates fixture-only tool adapters, builds per-skill Smelt cases, loads agent/judge/challenger clients from role-based `.env` settings, and saves JSON/Markdown reports.

**Tech Stack:** Python ≥3.12, uv, Smelt v0.5.1 from Git commit `e0f5c289786cc6dafa727fd924a6b6c4f47b7d8b`, pytest, Porto backend package.

**Spec:** `docs/superpowers/specs/2026-10-11-porto-agent-skill-eval-design.md`

## Global Constraints

- Load skill text from `porto_chatbot.agent_sdk.skills`; do not maintain prompt snapshots.
- All evaluation tools return fixture data only; no external network, Porto service, or write-capable tool is available to an agent.
- Agent and judge roles are separately configured with `SMELT_AGENT_*` and `SMELT_JUDGE_*`; challenge probes always run and use the judge role when no challenger model is configured.
- Commit `eval/.env.template`, create `eval/.env` with empty values, and verify Git ignores the actual `.env` while tracking the template.
- Never print environment values or place a real credential in source, tests, reports, or command output.
- Keep the preflight path credential-free and distinguish lint quality failures from harness/setup errors.
- Improve Porto skill prompts when evaluation evidence identifies concrete behavior gaps; leave the existing RAG/workflow evaluation suites untouched.

## Review Focus

- `.env.*` is ignored by the root ignore file; test that the template is force-included and the actual env remains ignored.
- Smelt challenge falls back to the judge when no challenger model is configured; verify challenge runs and is gated in `full`.
- Current skills can score below Smelt's lint threshold; test preflight completion separately from lint findings.
- A skill selection typo must produce a clear nonzero CLI result without running any LLM.
- A tool fixture must not write files or make network calls; test the complete registered tool inventory.

## File Structure

- `eval/pyproject.toml`, `eval/uv.lock`: standalone uv project and pinned Git/local dependencies.
- `eval/.gitignore`, `eval/.env.template`, `eval/.env`: local config, venv/report ignores, safe defaults.
- `eval/README.md`: setup, configuration, single/all skill commands, report interpretation.
- `eval/src/porto_skill_eval/{__init__,__main__,cli,config,skills,tools,cases,runner}.py`: source export, role config, fixtures, suites, and orchestration.
- `eval/src/porto_skill_eval/fixtures/`: small static PRD/spec/session-memory inputs used by the cases.
- `eval/tests/`: offline tests for configuration, exported skill inventory, case coverage, CLI, and report orchestration.
- `eval/reports/`: generated Markdown/JSON, ignored by Git.

---

### Task 1: Create the isolated uv project and safe configuration

**Files:**
- Create: `eval/pyproject.toml`
- Create: `eval/.gitignore`
- Create: `eval/.env.template`
- Create: `eval/.env`
- Create: `eval/README.md`
- Create: `eval/tests/test_config.py`
- Modify: `.gitignore` only if needed to explicitly track `eval/.env.template`

**Interfaces:**
- Produces an installable `eval` Python project with `porto-chatbot` as an editable local source and Smelt as a full-revision Git dependency.
- Env loader returns role configurations without exposing secret values; required roles are agent and judge, challenger is optional.

- [x] **Step 1: Write failing config and ignore tests**

Test `eval/.env.template` contains the documented role keys, `eval/.env` has matching keys with empty values, Git ignores `.env` but tracks `.env.template`, and the root is unchanged by env loading.

- [x] **Step 2: Run tests and verify they fail for missing eval files**

Run: `cd eval && uv run --with pytest pytest tests/test_config.py -q`
Expected: collection/setup fails because the project/config files do not exist.

- [x] **Step 3: Implement project metadata, ignore rules, env files, and README**

Pin Smelt to the full commit, resolve Porto from `../backend` through `[tool.uv.sources]`, set Python `>=3.12`, include pytest as a dev dependency, and explicitly unignore `eval/.env.template` after ignoring env files.

- [x] **Step 4: Lock/sync and rerun config tests**

Run: `cd eval && uv lock && uv sync && uv run pytest tests/test_config.py -q`
Expected: lock resolves the expected Smelt revision; all config/ignore assertions pass.

---

### Task 2: Export current skills and define fixture-only behavior cases

**Files:**
- Create: `eval/src/porto_skill_eval/skills.py`
- Create: `eval/src/porto_skill_eval/tools.py`
- Create: `eval/src/porto_skill_eval/cases.py`
- Create: `eval/src/porto_skill_eval/fixtures/prd/payment_platform.md`
- Create: `eval/src/porto_skill_eval/fixtures/spec/payment_api.md`
- Create: `eval/src/porto_skill_eval/fixtures/session_facts.json`
- Create: `eval/tests/test_skills.py`
- Create: `eval/tests/test_cases.py`

**Interfaces:**
- `export_skills(destination: Path) -> dict[str, Path]` calls Porto's production `deploy_skills` and returns a mapping for all five skill directories.
- `build_tools() -> list[smelt.Tool]` returns only deterministic Porto-shaped read tools.
- `build_cases(judge: LLMClient) -> dict[str, tuple[SmeltCase, ...]]` returns at least two cases per skill: intended behavior and a boundary/restraint probe.

- [x] **Step 1: Write failing export and case inventory tests**

Assert exact names `{prd-analysis, subsystem-decomposition, spec-generation, spec-evaluation, porto-memory}`, generated `SKILL.md` names/frontmatter match source definitions, every skill has at least two cases, and semantic judge expectations set `include_input=True` and `include_trace=True` plus direct required/forbidden tool assertions where applicable.

- [x] **Step 2: Run tests and verify they fail because these interfaces are absent**

Run: `cd eval && uv run pytest tests/test_skills.py tests/test_cases.py -q`
Expected: import/collection failure for the missing modules.

- [x] **Step 3: Implement skill export and safe tool fixtures**

Use Porto's `SKILLS`/`deploy_skills`; fixture handlers return in-memory PRD, KB, spec, memory, and session-fact content. Do not register write, shell, HTTP, or production database tools.

- [x] **Step 4: Implement cases for all five skills**

Each case passes task input and tool trace to LLM-as-judge with `include_input=True` and `include_trace=True`. Add tool-call assertions for explicit source retrieval and prior-memory/facts use, and negative assertions for small talk/no-retrieval. Bound agent turns and relevant tool counts.

- [x] **Step 5: Run focused tests and verify all cases are structurally bound to the right skill**

Run: `cd eval && uv run pytest tests/test_skills.py tests/test_cases.py -q`
Expected: every source skill exports; all ten-or-more cases have expectations and only fixture tools.

---

### Task 3: Add preflight, full Smelt evaluation, and report output

**Files:**
- Create: `eval/src/porto_skill_eval/config.py`
- Create: `eval/src/porto_skill_eval/runner.py`
- Create: `eval/src/porto_skill_eval/cli.py`
- Create: `eval/src/porto_skill_eval/__main__.py`
- Create: `eval/tests/test_runner.py`
- Create: `eval/tests/test_cli.py`
- Create: `eval/tests/test_smelt_cli.py`

**Interfaces:**
- `load_clients() -> EvalClients` loads `agent`, `judge`, and optional `challenger` through Smelt `LLMConfig.from_role`; missing required models produce a clear error without echoing keys.
- `preflight() -> PreflightResult` exports skills, validates case inventory and fixture-tool safety, and returns lint reports without requiring model config. It constructs judge expectations with a `ScriptedLLM` that is never invoked.
- `evaluate(skill_name: str | None, output_dir: Path) -> list[SkillEvaluation]` uses `evaluate_skill(...).with_cases(...).with_times(3)` and saves one Markdown and one JSON report per evaluated skill.
- CLI: `python -m porto_skill_eval preflight`; `python -m porto_skill_eval evaluate [--skill NAME] [--output DIR]`.

- [x] **Step 1: Write failing runner/CLI tests with injected ScriptedLLM clients**

Assert preflight succeeds with no model env using a non-networking `ScriptedLLM` only to construct judge expectations, five Smelt lint reports are returned even when individual grades are F, full evaluation rejects missing agent/judge model names, a focused skill selector rejects unknown names, and an injected deterministic evaluation writes both report formats without network calls.

- [x] **Step 2: Run tests and verify they fail for missing runner/CLI**

Run: `cd eval && uv run pytest tests/test_runner.py tests/test_cli.py -q`
Expected: imports fail for the absent runner and CLI.

- [x] **Step 3: Implement role configuration and safe diagnostics**

Resolve models with Smelt role configuration; validate names before building clients; diagnostics may list missing variable names but never values.

- [x] **Step 4: Implement preflight and evaluation orchestration**

Use the same exported skill paths for Smelt lint and behavior; always run challenge probes, using the judge as challenger fallback when no separate challenger model is configured. Keep challenge probes separate from the overall grade and gate them independently. Write reports to a timestamped run directory under `eval/reports/`.

- [x] **Step 5: Implement CLI and run focused tests**

Run: `cd eval && uv run pytest tests/test_runner.py tests/test_cli.py -q`
Expected: preflight requires no key; injected clients complete an evaluation and create Markdown/JSON; invalid skill and missing-model cases exit nonzero with actionable messages.

---

### Task 4: Verify the complete offline suite and documented user workflow

**Files:**
- Modify: `eval/README.md`
- Test: all `eval/tests/`

- [x] **Step 1: Run all eval tests**

Run: `cd eval && uv run pytest -q`
Expected: all offline tests pass with no LLM credentials.

- [x] **Step 2: Run preflight and inspect all emitted skill lint reports**

Run: `cd eval && uv run python -m porto_skill_eval preflight`
Expected: command exits 0, discovers exactly five current Porto skills and their cases, and reports each lint score; low skill scores do not make preflight fail.

- [x] **Step 3: Verify Smelt CLI discovery from a temporary export**

Run: `cd eval && uv run pytest tests/test_smelt_cli.py -q`
Expected: the test exports into a `TemporaryDirectory`, invokes `uv run smelt validate <temp>/.claude/skills --format markdown --fail-under 0`, and asserts the report contains all five skills and their scores. The CLI may exit 1 because F-grade reports fail Smelt's `passed` gate even at score threshold 0; the test treats that as a skill-quality result, not a harness failure.

- [x] **Step 4: Verify missing-model behavior and secret filtering**

Run full evaluation with role model variables removed, capture output, and assert it lists missing role/model configuration but contains no `.env` key values.
Expected: no LLM request occurs; exit is nonzero and actionable.

- [x] **Step 5: Recheck Git tracking and the final diff**

Run: `git check-ignore eval/.env` and `git check-ignore -q eval/.env.template` (the second command must return nonzero), followed by `git diff --check` and `git status --short`.
Expected: actual `.env` is ignored, template is trackable, no generated report/venv is in the diff, and the diff has no whitespace errors.

---

## Self-Review

- Spec coverage: all six acceptance criteria map to project/config setup (Task 1), source export and cases (Task 2), preflight/full evaluation (Task 3), and end-to-end offline checks (Task 4).
- Review Focus: all five risks have a named test or command in Tasks 1, 3, or 4.
- Interface consistency: `build_cases(judge)` creates unbound cases with input/trace-aware judge expectations; `preflight` supplies an unused `ScriptedLLM` for construction, and `evaluate` binds the cases to the requested exported skill using the supplied agent model and fixture tools.
- Scope: evaluation evidence led to targeted production skill prompt improvements; existing Porto RAG/workflow evals remain untouched.
