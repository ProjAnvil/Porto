---
description: Resume an interrupted or paused workflow
---

## User Input

```text
$ARGUMENTS
```

## Goal

Resume a specific workflow by ID. This is useful when:
- A workflow was interrupted
- User wants to return to a previous workflow
- Multiple workflows exist and user wants to switch

## Outline

### Step 1: List Resumable Workflows or Resume Specific One

If `$ARGUMENTS` is empty, list resumable workflows:

```bash
python3 {skills_scripts_dir}/porto_workflow.py resume
```

Parse the JSON response and display:

```
Usage: /porto.resume <workflow_id>

Resumable Workflows:

┌─────────────┬─────────────────────┬──────────┬───────────┬─────────────┐
│ ID (short)  │ Project             │ Step     │ Status    │ Updated     │
├─────────────┼─────────────────────┼──────────┼───────────┼─────────────┤
│ a7f3c8b1    │ E-Commerce Platform │ 2/4      │ paused    │ 2024-01-15  │
│ b2e4d6f8    │ Payment Gateway     │ 1/4      │ in_progr  │ 2024-01-14  │
└─────────────┴─────────────────────┴──────────┴───────────┴─────────────┘

To resume a workflow:
  /porto.resume a7f3c8b1
```

If a workflow ID is provided:

```bash
python3 {skills_scripts_dir}/porto_workflow.py resume --workflow "{WORKFLOW_ID}"
```

Supports both full UUID and short ID (first 8 characters).

### Step 2: Display Resume Status

Parse the JSON response from the resume command:

```
═══════════════════════════════════════════════════════════════
📋 Resuming Workflow: {workflow_id}
═══════════════════════════════════════════════════════════════

Project: {project_name}
Status:  {status}

Resume from Step {resume_from_step}: {step_name}
Skill: {skill}
Prerequisites: {prerequisites_ok}

Workspace: {workspace}

───────────────────────────────────────────────────────────────
Resuming...
───────────────────────────────────────────────────────────────
```

### Step 3: Resume at Appropriate Step

Based on `resume_from_step` in the JSON response, call the corresponding skill:

#### Case 1: Step 1 (understanding)
- Re-execute `prd-decomposition` skill
- Mark step 1 as started:
  ```bash
  python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 1
  ```

#### Case 2: Step 2 (subsystem_identification)
- Load Step 1 output
- Ask user if they want to review Step 1 first or continue directly
- If continuing, execute `subsystem-identification` skill

#### Case 3: Step 3 (subsystem_context_generation)
- Show Step 2 summary and subsystem list
- Execute `subsystem-context-generation` skill

#### Case 4: Step 4 (subsystem_specification)
- Show Step 3 summary
- Execute `subsystem-specification` skill

#### Case 5: Workflow Already Complete
```
ℹ️ This workflow is already complete.

Generated outputs available at:
  {workspace}

View results:
  ls {workspace}/step4/

Start a new workflow:
  /porto.gen <prd_file_path>
```

### Step 4: Error Handling

**Workflow Not Found**:
```
❌ Workflow not found: {provided_id}

No workflow matches this ID.

List resumable workflows:
  /porto.resume
```

**Prerequisites Not Met**:
```
⚠️ Prerequisites not satisfied for Step {N}

Previous steps may have incomplete outputs.
Check workflow status:
  /porto.status {workflow_id}
```

## Notes

- Supports partial UUID matching (minimum 8 characters)
- Uses `skills/scripts/porto_workflow.py` for all state management
- Validates workflow integrity before resuming
- Shows progress summary on resume
- Can resume at any step boundary
