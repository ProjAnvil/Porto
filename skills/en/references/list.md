---
description: List all Porto workflows with filtering options
---

## User Input

```text
$ARGUMENTS
```

## Goal

Query and display all workflows in the Porto system using the Python workflow manager.

## Outline

### Step 1: Parse Arguments and Call Script

Map `$ARGUMENTS` to script parameters:

| User Argument | Script Command |
|---------------|----------------|
| (none) | `python3 {scripts}/porto_workflow.py list` |
| `--all` | `python3 {scripts}/porto_workflow.py list --all` |
| `--recent <days>` | `python3 {scripts}/porto_workflow.py list --recent {days}` |
| `--status <status>` | `python3 {scripts}/porto_workflow.py list --status {status}` |
| `--name <keyword>` | `python3 {scripts}/porto_workflow.py list --name {keyword}` |
| `--step <n>` | `python3 {scripts}/porto_workflow.py list --step {n}` |
| `<workflow_id>` | `python3 {scripts}/porto_workflow.py status --workflow {id}` |

If invalid arguments:
```
Usage: /porto.list [options] [workflow_id]

Modes:
  /porto.list                           List workflows from last 3 days (default)
  /porto.list --recent <days>           List workflows from last N days
  /porto.list --all                     List all workflows
  /porto.list <workflow_id>             Show specific workflow details
  /porto.list --status <status>         Filter by status
  /porto.list --name <keyword>          Filter by project name
  /porto.list --step <n>                Filter by current step (1/2/3/4)

Examples:
  /porto.list                           # Last 3 days
  /porto.list --recent 7                # Last 7 days
  /porto.list a7f3c8b1                  # Specific workflow
  /porto.list --status in_progress      # Only in-progress
  /porto.list --step 2                  # Workflows at step 2
```

### Step 2: Display List View

Parse the JSON output from the script and render:

```
╔═══════════════════════════════════════════════════════════════╗
║                    Porto Workflows                            ║
╚═══════════════════════════════════════════════════════════════╝

Showing: Last 3 days | Total: {total} workflows

┌──────────────┬───────────────────────┬──────┬───────────┬────────────┐
│ ID (short)   │ Project               │ Step │ Status    │ Created    │
├──────────────┼───────────────────────┼──────┼───────────┼────────────┤
│ a7f3c8b1     │ E-Commerce Platform   │ 4/4  │ ✅ done   │ 2024-01-15 │
│ b2e4d6f8     │ Payment Gateway       │ 2/4  │ 🔄 in_pro │ 2024-01-14 │
│ c5g7h9i1     │ Inventory System      │ 1/4  │ ⏸️ paused │ 2024-01-13 │
└──────────────┴───────────────────────┴──────┴───────────┴─────────────┘

Status Legend:
  ✅ completed  🔄 in_progress  ⏸️ paused  ❌ failed

Statistics:
  Total: {total} | Completed: {completed} | In Progress: {in_progress} | Paused: {paused} | Failed: {failed}

Commands:
  View details:     /porto.status <id>
  Resume workflow:  /porto.resume <id>
  Start new:        /porto.gen <prd_file>
```

### Step 3: Display Specific Workflow (when workflow_id provided)

If a workflow_id was given, the script returns detailed status. Render it as described in `status.md`.

### Step 4: Empty State

If no workflows found:
```
📁 No workflows found

To create your first workflow:
  /porto.gen <prd_file_path>

Example:
  /porto.gen docs/requirements.md
```

## Status Icons

| Status | Icon |
|--------|------|
| `completed` | ✅ |
| `in_progress` | 🔄 |
| `paused` | ⏸️ |
| `failed` | ❌ |

## Notes

- All data comes from `skills/scripts/porto_workflow.py`
- Workflow ID can be partial (first 8 characters minimum)
- Default shows last 3 days
- Statistics summary included
