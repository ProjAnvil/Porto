---
name: porto
description: |
  Porto - AI-native PRD Decomposition System.
  Transforms business requirements into executable subsystem specifications.

  Subcommands:
  - gen <file_paths> [--name <project_name>]: Start a new PRD decomposition workflow
  - continue: Continue to the next step in the current workflow
  - resume <workflow_id>: Resume an interrupted or paused workflow
  - status [workflow_id]: View detailed status of a workflow
  - list [options]: List all Porto workflows with filtering options

  Use this skill when users need to:
  - Decompose PRD documents into subsystem specifications
  - Analyze business requirements and identify subsystems
  - Generate interaction diagrams and subsystem context
  - Manage PRD decomposition workflows

  Triggers: prd decomposition, requirement breakdown, subsystem design, workflow management
---

# Porto - PRD Decomposition System

AI-native PRD decomposition system that transforms business requirements into executable subsystem specifications through a 4-step interactive workflow.

## Command Parsing

Parse the first argument as subcommand:

```
/porto gen <file_paths> [--name <project_name>]
/porto continue
/porto resume <workflow_id>
/porto status [workflow_id]
/porto list [options]
```

| Subcommand | Function | Reference |
|------------|----------|-----------|
| `gen` | Start new workflow | Read `references/gen.md` |
| `continue` | Continue to next step | Read `references/continue.md` |
| `resume` | Resume workflow | Read `references/resume.md` |
| `status` | View workflow status | Read `references/status.md` |
| `list` | List workflows | Read `references/list.md` |

## Execution Flow

1. **Parse user input**, extract subcommand and arguments
2. **Load corresponding reference file**:
   - `gen` -> `references/gen.md`
   - `continue` -> `references/continue.md`
   - `resume` -> `references/resume.md`
   - `status` -> `references/status.md`
   - `list` -> `references/list.md`
3. **Execute subcommand logic**

## Core Resources

All workflow execution uses these resources (located within this skill):

| Resource | Path | Description |
|----------|------|-------------|
| Step 1 Skill | `references/prd-decomposition.md` | PRD analysis and understanding |
| Step 2 Skill | `references/subsystem-identification.md` | Subsystem identification |
| Step 3 Skill | `references/subsystem-context-generation.md` | Context diagram generation |
| Step 4 Skill | `references/subsystem-specification.md` | Specification generation |
| Knowledge Retrieval | `references/knowledge-retrieval.md` | Knowledge base search |
| Config File | `~/.porto/config.json` | Workflow configuration |
| Workflow Storage | `~/.porto/workflows/` | All workflow data |

## 4-Step Workflow

```
Step 1: PRD Decomposition       -> md/step1_understanding.md
Step 2: Subsystem Identification -> md/step2_subsystems.md
Step 3: Context Generation       -> md/step3_context.md (Mermaid diagrams)
Step 4: Spec Generation          -> md/step4/{subsystem}/REQUIREMENTS.md
```

## Workflow Output Structure

Each workflow generates the following files:

```
~/.porto/workflows/{workflow_id}/
├── workflow.json
├── current_step
├── inputs/
│   └── {prd_files}
└── md/
    ├── step1_understanding.md
    ├── step2_subsystems.md
    ├── step3_context.md
    └── step4/
        └── {subsystem}/REQUIREMENTS.md
```

## Design Principles

1. **Interactive Workflow** - User can review and edit between each step
2. **Knowledge-Driven** - Leverages configured knowledge bases when available
3. **Domain-Driven Design** - Uses DDD principles for subsystem decomposition
4. **Traceability** - All outputs link back to source requirements
