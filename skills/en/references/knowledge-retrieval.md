---
name: knowledge-retrieval
description: |
  Retrieve and search architectural knowledge from configured knowledge bases.
  Use this skill when you need to reference existing system designs, find similar
  microservice patterns, or check for potential conflicts with current systems.
  Also use when user mentions "knowledge base", "existing systems", "reference",
  "similar system", or needs to understand current architecture landscape.
allowed-tools: Read, Glob, Grep, Bash
---

# Knowledge Retrieval

## Instructions

This skill retrieves architectural knowledge from knowledge bases configured in `~/.porto/config.json`. Porto supports multiple knowledge base sources — each is a directory containing analysis documents, code, or architecture references.

### 1. Load Knowledge Base Configuration

```bash
cat ~/.porto/config.json | python3 -c "
import json, sys
config = json.load(sys.stdin)
for kb in config.get('knowledge_bases', []):
    if kb.get('enabled', True):
        print(f\"{kb['name']} ({kb['type']}): {kb['path']}\")
"
```

If no knowledge bases are configured or none are enabled, skip retrieval and note:
```
⚠️ No knowledge bases configured. Proceeding without knowledge base references.
```

### 2. Retrieval Strategies

For each enabled knowledge base of type `directory`:

#### Strategy 1: Exact Match (Priority)

When the PRD explicitly mentions integration with an existing system:

1. Search for `{kb_path}/{system_name}/` directory
2. If found, read analysis documents: `README.md`, `SUMMARY.md`, `ARCHITECTURE.md`, `FILE_INDEX.md`
3. Extract: APIs, data models, architecture patterns, dependencies

#### Strategy 2: Similarity Match (Fallback)

When no exact match is found:

1. Search across all subdirectories in the knowledge base for relevant keywords
2. Match by:
   - Business domain (e.g., "user management", "order processing", "notification")
   - Technology stack (e.g., "Go", "Java Spring", "Python FastAPI")
   - Architectural pattern (e.g., "event-driven", "CQRS", "microservices")

#### Strategy 3: Hybrid (Recommended)

Combine both strategies:
1. First attempt exact match across all knowledge bases
2. If no match or partial match, supplement with similarity search
3. Return top N most relevant references (N from config `knowledge_retrieval.max_references`)

### 3. Search Commands

```bash
# List contents of a knowledge base
ls -la {kb_path}/

# Search for systems by keyword across a knowledge base
grep -r "keyword" {kb_path}/*/SUMMARY.md 2>/dev/null
grep -r "keyword" {kb_path}/*/README.md 2>/dev/null

# Search for specific patterns
find {kb_path} -name "*.md" | xargs grep -l "keyword" 2>/dev/null
```

If the knowledge base has a `repos_path` configured (e.g., for code repositories):
```bash
# Search code repositories
ls {kb_repos_path}/
grep -r "pattern" {kb_repos_path}/{subsystem}/ 2>/dev/null
```

### 4. Knowledge Base Types

| Type | Description | Search Method |
|------|-------------|---------------|
| `directory` | Local directory with analysis docs/code | File system search (grep, find, ls) |
| `db` | Database-backed knowledge store | ⚠️ Not yet supported |

## Output Format

When retrieving knowledge, structure the output as:

```markdown
## Knowledge Base Reference

### Sources Consulted

| Knowledge Base | Type | Path | Systems Found |
|---------------|------|------|---------------|
| scv | directory | ~/.scv/analysis | 2 |
| internal-docs | directory | ~/company/docs | 1 |

### Matched Systems

| System | Match Type | Relevance | Source KB |
|--------|------------|-----------|----------|
| imed-process | Exact | 100% | scv |
| ircs-notice | Similar | 75% | scv |

### Reference Details

#### imed-process

**Source**: scv → ~/.scv/analysis/imed-process/

**Key Architectural Patterns**:
- Event-driven communication
- CQRS for order processing
- PostgreSQL + Redis caching

**Relevant APIs**:
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/v1/orders | Create order |

**Data Models**:
- Order (core entity)
- OrderItem (line items)
- OrderStatus (state machine)

**Integration Points**:
- Publishes: OrderCreated, OrderCompleted
- Consumes: PaymentProcessed, InventoryReserved
```
- Async queue processing

### Recommendations

Based on knowledge base analysis:

1. **Reuse**: Adopt imed-process's event schema for consistency
2. **Extend**: ircs-notice's notification pattern can be adapted
3. **New**: User authentication requires new implementation
```

## Configuration

The retrieval behavior can be configured in `~/.porto/config.json`:

```json
{
  "knowledge_retrieval": {
    "enabled": true,
    "match_strategy": "hybrid",
    "similarity_threshold": 0.6,
    "max_references": 5
  }
}
```

## Fallback Behavior

If no knowledge bases are configured or all configured paths are empty:

1. Log a warning message
2. Proceed without knowledge base references
3. Use generic best practices for architecture design
4. Suggest configuring knowledge bases in `~/.porto/config.json`
