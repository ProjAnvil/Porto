# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases before `0.12.0` predate this file; see the [tag list](https://github.com/ProjAnvil/Porto/tags)
for their commit history.

## [Unreleased]

### Fixed

- The `eval` workflow had never passed. The `unit` job could not collect
  `tests/rag_eval` because it did not install the `eval` extra, and the
  `nightly` job crashed at import time because missing secrets were exported as
  empty strings (`LANGCHAIN_AGENT_PROVIDER=''` fails `Settings` validation).
  The nightly job is now skipped until the `LANGCHAIN_API_KEY` secret is set,
  and secrets that are not set are left unset so `Settings` keeps its defaults.
- `test_chat_stream_native_streaming_when_llm_enabled` left a title-generation
  thread running that called the real OpenAI endpoint with a fake key and was
  still alive in later tests, which made the retry-count assertions in
  `test_llm_governance` fail.
- The spec baseline gate always skipped in CI. The per-test environment
  isolation in `tests/conftest.py` removes `LANGCHAIN_*` from the process
  environment, and the test only restored them from `.env` files, which CI
  does not have.
- `test_advance_past_first_checkpoint_reaches_identify` was flaky on slow CI
  runners. `advance` answers before its worker thread marks the workflow as
  running, so the test could read the pre-advance `awaiting_input` snapshot
  and assert that the run had stopped at `understand`. It now waits until the
  resumed run settles past that checkpoint.

### Tests

- Comments and docstrings in the test suites are now written in English
  (`backend/tests`, `frontend/e2e`, `frontend/playwright.config.ts`). String
  literals such as test data and assertion messages are unchanged.

## [0.12.0] — 2026-10-19

Porto can now reason over the **code structure graph** that
[SCV](https://github.com/ProjAnvil/SCV) emits as a Vectra snapshot
(`~/.scv/analysis/<repo>/structure.vg`). Until now Porto grounded specs only in
SCV's Markdown documents; it can now also ground them in the actual dependency
graph — which components exist, what a change reaches, and where the coupling
is tight enough to be one subsystem.

The graph is a **second retrieval channel**, not a replacement for the vector
store. Vectra is a graph engine with no embedding or similarity search, so
semantic retrieval stays with the existing embedding + BM25 path.

### Added

- **Structure-graph support behind a new `porto_chatbot.graph` package.** A
  `StructureGraph` protocol with two interchangeable backends, selected at
  runtime by a capability ladder:
  - **Tier 1** — an MCP client over stdio that talks to `scv-graph-mcp` or
    `vectra-mcp` and unlocks the full Cypher subset (aggregation, variable-length
    impact queries, `ORDER BY` on numeric properties).
  - **Tier 0** — a dependency-free pure-Python decoder for Vectra's v3 snapshot
    format, so the feature works on machines with no Go toolchain at all.
  - Both degrade to an empty graph. Nothing in this path raises to a caller:
    a missing binary, an unreadable snapshot, a corrupt file or an oversized
    graph all fail open and leave the existing retrieval behaviour untouched.
- **Snapshot decoder** for Vectra v3 (`"VCTR"` header, CRC-32 IEEE trailer) with
  lazy property decoding — measured at 0.32 s and ~225 MB for a 220k-node graph,
  and 40 % faster with lazy decoding because most node properties are never read.
- **Graph primitives**: `find` (anchor discovery), `neighbors`, `bfs`, `cycles`
  and `lookup`. `bfs` defaults to `INJECTS`-only edges: with both node surfaces
  loaded, an unfiltered walk escapes from a `Bean` through `CONTAINS` into every
  `Method` in the repository.
- **Dependency-cycle grading that matches SCV exactly.** Cycles are computed
  with an iterative Tarjan SCC pass and graded `fatal` or `tolerated` using
  SCV's own rule (only `INJECTS` edges count, `via ∈ {constructor,
  lombok-constructor, beanMethod}` is fatal, and a cycle with no `INJECTS` edge
  is not reported at all). Porto will not report a cycle SCV disagrees with.
- **Graph context injected at three points in the pipeline:**
  - `retrieve` — the impact surface is appended as its own source chunk, on all
    three return paths including the LLM fail-open branch.
  - `identify` — **the largest behavioural gain in this release.** This node
    previously reasoned from language alone: `state["sources"]` never reached it
    and the step passes no tools. It now receives repository size, SCCs and
    cycles, a component stereotype census, and the largest types by `loc`. A
    strongly connected cluster is a natural subsystem boundary.
  - `generate` — each subsystem's spec prompt now carries what that subsystem
    actually touches.
- **`search_code_graph` agent tool**, registered on both the Langchain and
  Agent SDK backends, letting the model confirm which components exist and how
  they are named. The model passes keywords; the Cypher templates stay in
  Porto, so it cannot emit an invalid query and retry it in a loop.
- **Eight new settings** (also documented in `.env.example` and
  `docs/deployment.md`): `graph_enabled`, `graph_mcp_enabled`,
  `graph_mcp_binary`, `graph_mcp_timeout`, `graph_max_nodes`,
  `graph_max_depth`, `graph_edge_labels`, `graph_context_max_nodes`.

### Changed

- **The spec rubric gains a seventh dimension, `module_coverage`** — whether
  the modules and components in the graph's impact surface are actually named
  in the spec. The previous six dimensions were all text-semantic; none of them
  could tell whether a spec covered the code it was supposed to change.
- The rubric maximum moves from 12 to 14 and is now **derived** from the
  dimension count. The old maximum was hardcoded in six places (the rubric
  comment, the critique schema, the prompt text, the score clamp, `settings.py`
  and `models/payload.py`); adding a dimension is now a one-list edit.
- `spec_refine_pass_score` defaults to **12** instead of 10, keeping roughly
  the same pass proportion (10/12 → 12/14). See the upgrade notes.
- Test isolation: an autouse fixture points `kb_dirs` at a temporary directory,
  so tests never load a developer's real `~/.scv/analysis` snapshots.

### Fixed

- `specs/` had no module-level logger. Failures in the new prompt assembly were
  previously invisible; the package now logs through `get_component_logger`.
- `identify` uses `agent.logger` rather than a bare `logger`. The module has no
  module-level logger, and a bare `logger` call raises `NameError` that the
  surrounding `except Exception` would have swallowed into a silent failure.

### Upgrade notes

- **Review `spec_refine_pass_score` if you have overridden it.** The rubric
  maximum is now 14. An override left at 10 will pass specs noticeably more
  easily than before; raise it proportionally.
- **Graph support is on by default but inert without SCV snapshots.** If SCV
  has not run perception for a repository, there is no `structure.vg`, and the
  tier ladder resolves to an empty graph on the first directory scan — no
  latency and no behaviour change. There is nothing to migrate.
- Set `PORTO_CHATBOT_GRAPH_ENABLED=false` to switch the channel off entirely.

### Compatibility

- Vectra snapshots are read-only from Porto's side. Porto never calls
  `inject_batch` or `seal`: SCV owns `structure.vg` and regenerates it on every
  run, so anything Porto wrote would be erased.
- Only v3 snapshots are decoded. Vectra itself still reads v1 and v2, but SCV
  only produces v3 and each extra format would cost another test suite.
- No new runtime dependencies. Tier 0 uses only the standard library; Tier 1
  uses `subprocess` and `json`.
- Nothing in `vector_stores/` was modified. Vectra does not implement the
  `VectorRepository` protocol — it has no embeddings, dimensions or top-k
  similarity — so Chroma and Milvus are unaffected.

### Tests

286 tests cover the decoder, the local graph, the MCP client, the tier ladder,
context rendering and end-to-end injection, including parity cases proving that
behaviour is unchanged when the graph is absent or disabled.

[Unreleased]: https://github.com/ProjAnvil/Porto/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/ProjAnvil/Porto/compare/v0.11.0...v0.12.0
