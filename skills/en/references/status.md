---
description: View detailed status of a workflow
---

## User Input

```text
$ARGUMENTS
```

## Goal

Display detailed status information for a specific workflow using the Python workflow manager.

## Outline

### Step 1: Parse Arguments

If `$ARGUMENTS` is empty:
```bash
python3 {skills_scripts_dir}/porto_workflow.py status
```
This returns the active (most recent in-progress) workflow.

If a workflow ID or `--full` is provided:
```bash
python3 {skills_scripts_dir}/porto_workflow.py status --workflow "{WORKFLOW_ID}"
python3 {skills_scripts_dir}/porto_workflow.py status --workflow "{WORKFLOW_ID}" --full
```

### Step 2: Display Status Report

Parse the JSON output from the script and render:

```
═══════════════════════════════════════════════════════════════
📋 Workflow Status Report
═══════════════════════════════════════════════════════════════

Workflow ID:     {workflow_id}
Project Name:    {project_name}
Created:         {created_at}
Last Updated:    {updated_at}
Status:          {status}
Progress:        {completed}/{total} steps ({progress_pct}%)

───────────────────────────────────────────────────────────────
📁 Workspace
───────────────────────────────────────────────────────────────

Location: {workspace}

Input Files:
  {input_files_list}

───────────────────────────────────────────────────────────────
📊 Step Progress
───────────────────────────────────────────────────────────────

For each step in the JSON response:

Step {N}: {name}
  Status:   {status_icon} {status}
  Output:   {output} {exists_check}
  Size:     {output_size}
  Started:  {started_at}
  Completed:{completed_at}

───────────────────────────────────────────────────────────────
🔗 Quick Actions
───────────────────────────────────────────────────────────────

Continue to next step:
  /porto.continue

Resume this workflow later:
  /porto.resume {workflow_id}

View all workflows:
  /porto.list
```

### Step 3: Full Preview Mode

If `--full` flag is provided, the JSON response includes a `previews` field with the first 50 lines of each output file. Display those:

```
───────────────────────────────────────────────────────────────
📄 Step 1 Preview (first 50 lines)
───────────────────────────────────────────────────────────────

{preview content}

───────────────────────────────────────────────────────────────
📄 Step 2 Preview (first 50 lines)
───────────────────────────────────────────────────────────────

{preview content}
```

### Step 4: Error Handling

**Workflow Not Found**:
```
❌ Workflow not found: {workflow_id}

List available workflows:
  /porto.list --all
```

**No Active Workflow**:
```
ℹ️ No active workflow found

Start a new workflow:
  /porto.gen <prd_file_path>

View all workflows:
  /porto.list --all
```

## Notes

- All state is managed by `skills/scripts/porto_workflow.py`
- Default shows most recent in-progress workflow
- `--full` flag includes content previews (first 50 lines per output)
- Displays file sizes and modification times
- Includes summary statistics from each step
