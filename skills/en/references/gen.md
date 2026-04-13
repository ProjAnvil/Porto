---
description: Start a new PRD decomposition workflow
---

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Goal

Initialize a new Porto workflow and execute Step 1 (Business Understanding). The workflow is interactive - after each step, the user can review and edit the output before continuing.

## Outline

### Step 1: Parse Arguments and Validate

Parse `$ARGUMENTS` to extract:

- **file_paths** (REQUIRED): One or more PRD file paths
- **project_name** (OPTIONAL): Custom project name (`--name` flag)

If no arguments:
```
❌ Usage: /porto.gen <file_path1> [file_path2 ...] [--name <project_name>]

Description:
  Start a new Porto workflow to decompose business requirements

Arguments:
  <file_path>     Path to PRD document(s)
                  Supported formats: .md, .pdf, .txt, .docx

Options:
  --name          Custom project name (default: extracted from first file)

Examples:
  # Single PRD file
  /porto.gen docs/requirements.md

  # Multiple PRD files
  /porto.gen docs/backend_prd.md docs/frontend_prd.md

  # With custom project name
  /porto.gen docs/prd.pdf --name "E-Commerce Platform v2"
```

**Validation**:
1. Verify all files exist and are readable
2. If any file missing, show error with available files

### Step 2: Initialize Workflow via Python Script

Use the workflow manager script to create the workspace:

```bash
python3 {skills_scripts_dir}/porto_workflow.py init \
  --name "{workflow_name}" \
  --inputs "{file_path1},{file_path2}" \
  --project "{project_name}"
```

The script will:
- Generate a UUID workflow ID
- Create directory structure at `~/.porto/workflows/{ID}/`
- Copy input files to `inputs/`
- Initialize `workflow.json` with step 1 as `in_progress`
- Return the workflow ID and workspace path

Parse the JSON output to extract `workflow_id` and `workspace`.

**Display initialization**:
```
╔═══════════════════════════════════════════════════════════╗
║              Porto Workflow Initialized                    ║
╚═══════════════════════════════════════════════════════════╝

📋 Workflow ID: {WORKFLOW_ID}
📁 Workspace: ~/.porto/workflows/{WORKFLOW_ID}/
📄 Project: {project_name}

Input Files:
  1. {file_name_1}
  2. {file_name_2}

🚀 Starting Step 1: Business Requirements Understanding...
```

### Step 3: Execute Step 1 - Business Understanding

Load and execute the `prd-decomposition` skill:

1. **Read input files** from `inputs/` directory
2. **Analyze PRD** using the skill's instructions
3. **Generate** `step1_understanding.md` in the workspace directory

**Important**: Follow the exact output format defined in `skills/prd-decomposition.md`

### Step 4: Mark Step Complete via Python Script

```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 1 \
  --output "step1_understanding.md" \
  --summary '{"features": {N}, "p0": {n}, "p1": {n}, "p2": {n}, "hints": {N}, "questions": {N}}'
```

**Display completion**:
```
═══════════════════════════════════════════════════════════════
✅ Step 1 Complete: Business Requirements Understanding
═══════════════════════════════════════════════════════════════

📄 Output: ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

Summary:
• {N} features identified (P0: {n}, P1: {n}, P2: {n})
• {N} subsystem hints detected
• {N} clarification questions raised

───────────────────────────────────────────────────────────────
Next Actions:
───────────────────────────────────────────────────────────────

1. Review the output:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

2. Edit if needed (optional):
   vim ~/.porto/workflows/{WORKFLOW_ID}/step1_understanding.md

3. Continue to Step 2:
   /porto.continue

💡 Tip: You can edit the document before continuing. Porto will
   use your edited version for the next step.
```

### Step 5: Wait for User Action

After Step 1 completes, **STOP and wait** for user to:
- Review the generated document
- Edit if necessary
- Run `/porto.continue` to proceed to Step 2

## Error Handling

**File Not Found**:
```
❌ Error: File not found

File: {file_path}

Please check the path and try again.
```

**Permission Error**:
```
❌ Error: Cannot create workflow directory

Run: mkdir -p ~/.porto/workflows && chmod 755 ~/.porto/workflows
```

**Invalid File Format**:
```
⚠️ Warning: File format may not be fully supported

File: {file_path}
Format: {extension}

Proceeding with analysis. For best results, use .md or .txt files.
```

## Notes

- All state management is handled by `skills/scripts/porto_workflow.py`
- `{skills_scripts_dir}` resolves to the scripts directory under the Porto skill installation
- Step 1 skill: `prd-decomposition`
- User can edit any generated file before continuing
- All outputs stored in workflow directory for traceability
