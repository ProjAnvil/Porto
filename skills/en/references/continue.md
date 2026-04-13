---
description: Continue to the next step in the current workflow
---

## User Input

```text
$ARGUMENTS
```

## Goal

Advance the current workflow to the next step. This command should be used after reviewing and optionally editing the output of the current step.

## Outline

### Step 1: Find Current Workflow via Python Script

```bash
python3 {skills_scripts_dir}/porto_workflow.py current --workflow "{WORKFLOW_ID}"
```

Or if no workflow ID is known, get the active one:

```bash
python3 {skills_scripts_dir}/porto_workflow.py status
```

If the status response has `status: "no_active_workflow"`:
```
❌ No active workflow found

To start a new workflow:
  /porto.gen <prd_file_path>

To resume a specific workflow:
  /porto.resume <workflow_id>
```

### Step 2: Advance to Next Step via Python Script

```bash
python3 {skills_scripts_dir}/porto_workflow.py advance --workflow "{WORKFLOW_ID}"
```

The script validates the current step is completed and returns the next step info.

Based on the `to_step` value in the JSON response:

| from_step | to_step | Skill to Execute |
|-----------|---------|------------------|
| 1 | 2 | `subsystem-identification` |
| 2 | 3 | `subsystem-context-generation` |
| 3 | 4 | `subsystem-specification` |
| 4 | - | Workflow Complete |

If `workflow_already_completed`:
```
ℹ️ Workflow already complete

Workflow ID: {WORKFLOW_ID}
Status: completed

To view results:
  ls ~/.porto/workflows/{WORKFLOW_ID}/step4/

To start a new workflow:
  /porto.gen <prd_file_path>
```

### Step 3: Execute Step 2 (if to_step = 2)

**Skill**: `subsystem-identification`

1. **Mark step as started**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 2
   ```

2. **Read prerequisite**: `step1_understanding.md`
3. **Invoke knowledge-retrieval skill** to check existing systems
4. **Generate**: `step2_subsystems.md`

**After generation, mark complete and record subsystems**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 2 \
  --output "step2_subsystems.md" \
  --summary '{"subsystems": [{"name": "imed-process", "type": "new"}, ...], "kb_refs": 3}'

python3 {skills_scripts_dir}/porto_workflow.py set-subsystems \
  --workflow "{WORKFLOW_ID}" \
  --subsystems '[{"name": "imed-process", "type": "new"}, ...]'
```

**Output**:
```
═══════════════════════════════════════════════════════════════
✅ Step 2 Complete: Subsystem Identification
═══════════════════════════════════════════════════════════════

📄 Output: ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

Summary:
• {N} subsystems identified
  - New: {n}
  - Extending existing: {n}
  - Reusing existing: {n}
• {N} knowledge base references matched

Identified Subsystems:
┌─────────────────┬──────────┬─────────────────────────────┐
│ Name            │ Type     │ Responsibility              │
├─────────────────┼──────────┼─────────────────────────────┤
│ imed-process    │ new      │ Order processing            │
│ ircs-notice     │ extend   │ Multi-channel notifications │
│ payment-gateway │ new      │ Payment integration         │
└─────────────────┴──────────┴─────────────────────────────┘

───────────────────────────────────────────────────────────────
Next Actions:
───────────────────────────────────────────────────────────────

1. Review subsystem definitions:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

2. Edit subsystem names or boundaries if needed (optional):
   vim ~/.porto/workflows/{WORKFLOW_ID}/step2_subsystems.md

3. Continue to Step 3 (generate interaction diagrams):
   /porto.continue
```

### Step 4: Execute Step 3 (if to_step = 3)

**Skill**: `subsystem-context-generation`

1. **Mark step as started**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 3
   ```

2. **Read prerequisites**: `step1_understanding.md`, `step2_subsystems.md`
3. **Search knowledge base repositories** for each subsystem
4. **Analyze** code patterns, APIs, events
5. **Generate**: `step3_context.md`

**After generation, mark complete**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 3 \
  --output "step3_context.md" \
  --summary '{"diagrams": {"sequence": 3, "state": 2, "flowchart": 1, "component": 2, "er": 1}, "kb_repos": 2}'
```

**Output**:
```
═══════════════════════════════════════════════════════════════
✅ Step 3 Complete: Subsystem Context Generation
═══════════════════════════════════════════════════════════════

📄 Output: ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

Generated Diagrams:
• {N} Sequence diagrams (business flows)
• {N} State machines (entity states)
• {N} Flowcharts (decision logic)
• {N} Component diagrams (architecture)
• {N} ER diagrams (data model)

Knowledge Base Repositories Analyzed:
• imed-process (45 files)
• ircs-notice (32 files)
• payment-gateway (not found - inferred)

───────────────────────────────────────────────────────────────
Next Actions:
───────────────────────────────────────────────────────────────

1. Review interaction diagrams:
   cat ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

2. Edit if needed (optional):
   vim ~/.porto/workflows/{WORKFLOW_ID}/step3_context.md

3. Continue to Step 4 (generate subsystem specifications):
   /porto.continue
```

### Step 5: Execute Step 4 (if to_step = 4)

**Skill**: `subsystem-specification`

1. **Mark step as started**:
   ```bash
   python3 {skills_scripts_dir}/porto_workflow.py step-start --workflow "{WORKFLOW_ID}" --step 4
   ```

2. **Read prerequisites**: `step1_understanding.md`, `step2_subsystems.md`, `step3_context.md`
3. **Check knowledge base** for reference patterns
4. **For each subsystem**, generate `step4/{subsystem_name}/REQUIREMENTS.md`

**After generation, mark complete**:
```bash
python3 {skills_scripts_dir}/porto_workflow.py step-complete \
  --workflow "{WORKFLOW_ID}" \
  --step 4 \
  --output "step4" \
  --summary '{"subsystems": [{"name": "imed-process", "capabilities": 5, "apis": 12}, ...]}'
```

**Output**:
```
═══════════════════════════════════════════════════════════════
✅ Step 4 Complete: Subsystem Specifications Generated
═══════════════════════════════════════════════════════════════

📁 Output Directory: ~/.porto/workflows/{WORKFLOW_ID}/step4/

Generated Specifications:
┌─────────────────┬────────────────┬─────────────────────────────────┐
│ Subsystem       │ File           │ Summary                         │
├─────────────────┼────────────────┼─────────────────────────────────┤
│ imed-process    │ REQUIREMENTS.md│ 5 capabilities, 12 APIs, 3 ents │
│ ircs-notice     │ REQUIREMENTS.md│ 3 capabilities, 8 APIs, 2 ents  │
│ payment-gateway │ REQUIREMENTS.md│ 4 capabilities, 10 APIs, 4 ents │
└─────────────────┴────────────────┴─────────────────────────────────┘

═══════════════════════════════════════════════════════════════
🎉 Workflow Complete!
═══════════════════════════════════════════════════════════════

Workflow ID: {WORKFLOW_ID}

Next Actions:
  • Review a subsystem spec:
    cat ~/.porto/workflows/{WORKFLOW_ID}/step4/imed-process/REQUIREMENTS.md

  • Share specifications with development teams

  • Begin implementation planning
```

### Step 6: Handle Errors

**Previous Step Not Complete** (from advance command error):
```
❌ Cannot advance: Step {N} status is '{status}', not 'completed'.
   Complete step {N} first.
```

**Missing Prerequisite File**:
```
❌ Missing prerequisite file

Expected: ~/.porto/workflows/{WORKFLOW_ID}/step{n-1}_*.md

The previous step may have failed. Try:
  /porto.resume {WORKFLOW_ID}
```

**Knowledge Base Not Available**:
```
⚠️ Warning: No knowledge base configured or available

Proceeding without knowledge base references.
For better results, configure knowledge bases in ~/.porto/config.json

Continue anyway? (Step 2 will use generic patterns)
```

## Notes

- All state management is handled by `skills/scripts/porto_workflow.py`
- The script ensures idempotent operations (safe to retry after crashes)
- User can edit outputs at any step before continuing
- Step 4 marks workflow as complete automatically
