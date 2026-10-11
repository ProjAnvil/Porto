# Porto Agent Skill Eval Design

## Goal

Add a reproducible evaluation project under `eval/` that uses Smelt to assess all five Porto Agent SDK skills. It must evaluate skill behavior, writing quality, and static lint, and let a developer run the suite after filling in their own LLM configuration.

## Context and requirements

- Porto defines its five skills and deploys their `SKILL.md` files from `backend/src/porto_chatbot/agent_sdk/skills.py`.
- Evaluation must load the current definitions from Porto rather than rely on copied prompt snapshots.
- Smelt is installed by uv from its Git repository and locked to a full commit revision (`e0f5c289786cc6dafa727fd924a6b6c4f47b7d8b`, v0.5.1).
- `eval/.env.template` is committed as a safe configuration example. `eval/.env` is created locally with empty values and ignored by Git.
- The suite does not call live Porto services or modify the Porto application data. Behavior cases use deterministic fixture-backed tools.
- No LLM credentials are currently configured in this environment. Offline validation must therefore cover setup, skill generation, cases, and Smelt lint; full model evaluation remains runnable after the developer fills `eval/.env`.

## Architecture

`eval/` is an isolated uv project with its own `pyproject.toml` and `uv.lock`. It depends on Smelt through a pinned Git revision and on Porto's local backend package so the runner can call the production skill deployment function. The uv project runs from `eval/`, making Smelt's automatic `.env` loading resolve to `eval/.env`.

The runner builds all five skills into a temporary directory by calling Porto's `SKILLS` / `deploy_skills` definitions. Each evaluation uses Smelt's `evaluate_skill` API with a separate agent model and judge model, Porto-shaped fixture tools, curated cases, three behavior samples by default, writing review, and static lint. Adversarial challenge is enabled when a challenger role is configured and reported as skipped otherwise. Each skill produces Markdown and JSON reports under `eval/reports/`; local reports and the uv environment are ignored.

## Behavior coverage

Each skill gets cases that exercise its intended work and at least one boundary or restraint:

- `prd-analysis`: extract stated goals, entities, constraints, and subsystem clues from a short PRD; distinguish source facts from unknowns and retrieve PRD / knowledge-base context when asked to inspect Porto's source material.
- `subsystem-decomposition`: return 2–6 cohesive `*-service` boundaries with responsibilities, entities, and dependencies; avoid inventing services unsupported by the PRD.
- `spec-generation`: produce all six required sections, concrete API details, fields, errors, and measurable acceptance criteria; use fixture knowledge-base conventions when the request calls for them.
- `spec-evaluation`: apply Porto's six-dimension, 0–12 rubric, ground findings in the supplied spec, give a verdict, and critique without rewriting.
- `porto-memory`: consult session facts and prior memory for references to confirmed decisions; answer small talk without calling memory or knowledge-base tools.

LLM-judged expectations include the input and tool trace. Direct tool-call expectations pin required and forbidden calls; budgets cap turns and tool calls. Cases use fixed fixture responses and have no file-write or network-capable tools.

## Configuration and commands

The template documents independent `SMELT_AGENT_*`, `SMELT_JUDGE_*`, and optional `SMELT_CHALLENGER_*` provider/model/base URL/API key settings. No real key is committed. The runner validates required model settings before starting a full run and never prints API key values.

The documented workflow is:

1. `cd eval && uv sync` installs the pinned toolchain.
2. `uv run python -m porto_skill_eval preflight` validates the local setup without contacting an LLM.
3. Fill `eval/.env` with the chosen agent and judge models.
4. `uv run python -m porto_skill_eval evaluate` evaluates all five skills and writes reports.
5. `uv run python -m porto_skill_eval evaluate --skill <name>` supports focused reruns.

## Verification

Automated offline checks verify the `.env` template and ignore rules, exact skill inventory and generated frontmatter, one case suite per skill, valid tool fixtures, required assertions, and Smelt lint discovery. A preflight command performs those checks and writes no credentials or reports. The full LLM run is validated structurally without credentials and is ready for the developer to run after configuration; its real-model scores cannot be claimed until that run occurs.

## Acceptance criteria

1. `uv lock` resolves the Smelt Git dependency to the specified commit and `uv sync` succeeds.
2. The local `eval/.env` exists, is ignored by Git, contains no filled credential, and matches the documented template keys.
3. Preflight exits successfully without credentials and confirms all five skills and case suites.
4. `smelt validate` discovers all five current generated skill documents and emits per-skill lint findings.
5. With valid agent and judge configuration, one command runs all five behavior/writing/lint evaluations and saves Markdown and JSON reports; one skill can be rerun by name.
6. Automated cases never call Porto services, use external state, or offer write-capable tools.

## Out of scope

- Changing Porto's skill prompts as part of this evaluation harness.
- Running or committing evaluations against a real LLM before the user configures credentials.
- Replacing Porto's existing RAG and workflow evaluation suites.
