# Porto - Business Requirement Decomposition System

> Transform business requirements into actionable subsystem specifications.

**English** | [中文](docs/README_zhcn.md)

## Project Governance

- [Contributing Guide](CONTRIBUTING.md)
- [Security Policy](SECURITY.md)
- [Code of Conduct](CODE_OF_CONDUCT.md)
- [License](LICENSE)

Porto is an AI-native system that decomposes Product Requirement Documents (PRDs) into detailed subsystem-level requirement specifications.

## Core Concept

Porto transforms business requirements through a **4-step workflow**:

1. **Understand** - Analyze PRD to extract business requirements
2. **Identify** - Define subsystem boundaries based on business capabilities
3. **Contextualize** - Generate interaction diagrams from knowledge base code analysis
4. **Specify** - Generate detailed requirement specs for each subsystem

```
PRD Document → Business Understanding → Subsystem ID → Context Diagrams → Subsystem Specs
                                                                          ↓
                                                            step4/{subsystem}/REQUIREMENTS.md
```

## Quick Start

```bash
# 1. Install Porto
./install.sh

# 2. (Optional) Configure knowledge bases in ~/.porto/config.json

# 3. Start a new workflow
/porto gen docs/requirements.md --name "My Project"

# 4. Review output, then continue
/porto continue

# 5. Repeat until complete (4 steps)
```

---

## Installation

### Quick Install (Recommended)

```bash
# Default (English)
./install.sh

# Chinese version
./install.sh --lang=zhcn

# Show help
./install.sh --help
```

The script will:
- Create `~/.porto` configuration directory
- Copy the configuration file
- Install the language-specific skill to `~/.claude/skills/porto`
- Install the `prd-analyst` agent to `~/.claude/agents/`

### Language Options

| Option | Description |
|--------|-------------|
| `--lang=en` | English version (default) |
| `--lang=zhcn` | Chinese version |

### Directory Structure After Installation

```
~/.claude/
├── agents/
│   └── prd-analyst.md         # Subagent for PRD analysis
└── skills/porto/
    ├── SKILL.md               # Skill entry point
    └── references/
        ├── gen.md
        ├── continue.md
        ├── resume.md
        ├── status.md
        ├── list.md
        ├── prd-decomposition.md
        ├── subsystem-identification.md
        ├── subsystem-context-generation.md
        ├── subsystem-specification.md
        └── knowledge-retrieval.md
```

---

## Available Commands

| Command | Description |
|---------|-------------|
| `/porto gen <files>` | Start a new PRD decomposition workflow |
| `/porto continue` | Continue to the next step |
| `/porto resume <id>` | Resume an interrupted workflow |
| `/porto status [id]` | View detailed workflow status |
| `/porto list [options]` | List all workflows with filters |

---

## Workflow Overview

Porto uses a **4-step interactive workflow**:

### Step 1: Business Understanding

**Input**: PRD document(s)
**Output**: `step1_understanding.md`

Analyzes and extracts:
- Business objectives and target users
- Core business processes
- Feature breakdown (P0/P1/P2 priority)
- Domain entities and relationships
- Non-functional requirements
- **Subsystem hints** for Step 2

### Step 2: Subsystem Identification

**Input**: `step1_understanding.md` + Knowledge Bases
**Output**: `step2_subsystems.md`

Identifies and defines:
- Subsystem boundaries (based on DDD bounded contexts)
- Subsystem responsibilities
- Dependencies between subsystems
- Preliminary API definitions
- Technology stack recommendations
- **References to existing systems** in configured knowledge bases

### Step 3: Context Generation

**Input**: `step1_understanding.md` + `step2_subsystems.md` + Knowledge Base Repositories
**Output**: `step3_context.md`

Analyzes code repositories from configured knowledge bases and generates:
- **Sequence diagrams** for business flows
- **State diagrams** for entity state machines
- **Flowcharts** for decision logic
- **Component diagrams** for system architecture
- **ER diagrams** for data models
- **Event catalog** (published/consumed events)
- **Integration contracts** (APIs, async events)

### Step 4: Subsystem Specification

**Input**: `step1_understanding.md` + `step2_subsystems.md` + `step3_context.md`
**Output**: `step4/{subsystem_name}/REQUIREMENTS.md` for each subsystem

Generates detailed specifications per subsystem:
- Business capabilities and acceptance criteria
- API contracts (endpoints, request/response schemas)
- Data model requirements (entities, relationships, indexes)
- Integration requirements (dependencies, events)
- Non-functional requirements (performance, security, scalability)
- Technology recommendations with rationale

---

## Output Structure

```
~/.porto/
├── config.json                    # Porto configuration
└── workflows/
    └── {workflow_id}/
        ├── workflow.json          # Workflow metadata
        ├── current_step           # Current step (1/2/3/4)
        ├── inputs/                # Original PRD files
        │   └── requirements.md
        ├── step1_understanding.md # Business requirements
        ├── step2_subsystems.md    # Subsystem definitions
        ├── step3_context.md       # Interaction diagrams
        └── step4/                 # Per-subsystem specs
            ├── imed-process/
            │   └── REQUIREMENTS.md
            ├── ircs-notice/
            │   └── REQUIREMENTS.md
            └── payment-gateway/
                └── REQUIREMENTS.md
```

### Step 3 Context Output Example

`step3_context.md` contains Mermaid diagrams:

```markdown
## Order Creation Flow

\`\`\`mermaid
sequenceDiagram
    participant Client
    participant Gateway as API Gateway
    participant Order as imed-process
    participant Inventory
    participant Payment
    participant MQ as Message Queue
    participant Notify as ircs-notice

    Client->>Gateway: POST /api/v1/orders
    Gateway->>Order: CreateOrder(request)
    Order->>Inventory: CheckStock(items)
    Inventory-->>Order: Stock Available
    Order->>Order: Create order entity
    Order->>MQ: Publish OrderCreated
    Order-->>Gateway: Order Created (201)

    MQ->>Payment: Process payment
    Payment->>MQ: Publish PaymentCompleted
    MQ->>Notify: Send confirmation
\`\`\`

## Order State Machine

\`\`\`mermaid
stateDiagram-v2
    [*] --> Created: Order placed
    Created --> Validated: Stock confirmed
    Validated --> Paid: Payment success
    Paid --> Shipped: Items dispatched
    Shipped --> Delivered: Delivery confirmed
    Delivered --> [*]
\`\`\`
```

### Step 4 Specification Example

`step4/imed-process/REQUIREMENTS.md`:

```markdown
# imed-process - System Requirements

## 1. Executive Summary

| Attribute | Value |
|-----------|-------|
| **Name** | imed-process |
| **Type** | extend |
| **Responsibility** | Order processing and management |
| **Owner** | Order Team |

## 2. Business Capabilities

| ID | Capability | Priority |
|----|------------|----------|
| BC-001 | Order creation | P0 |
| BC-002 | Order tracking | P0 |
| BC-003 | Order cancellation | P1 |

### BC-001: Order Creation

**Acceptance Criteria**:
- [ ] Support single and batch order creation
- [ ] Validate inventory before order confirmation
- [ ] Generate unique order number

## 3. API Requirements

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /api/v1/orders | Create order |
| GET | /api/v1/orders/{id} | Get order details |
| PUT | /api/v1/orders/{id}/status | Update order status |

## 4. Data Model Requirements

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| id | string | Yes | UUID |
| customerId | string | Yes | Customer reference |
| status | enum | Yes | created/paid/shipped/delivered |
| totalAmount | decimal | Yes | Order total |

## 5. Non-Functional Requirements

| Metric | Requirement |
|--------|-------------|
| Response Time | < 200ms (P95) |
| Availability | 99.9% |
| Throughput | 1000 req/sec |

**Reference**: Knowledge base analysis documents
```

---

## Knowledge Base Integration

Porto references configured knowledge bases for:

- **Step 2**: Identify if existing systems can fulfill requirements
- **Step 3**: Analyze actual code patterns from repository sources
- **Step 4**: Extract patterns and conventions for specification generation

### Configuration

Knowledge bases are configured in `~/.porto/config.json`:

```json
{
  "knowledge_bases": [
    {
      "name": "my-kb",
      "type": "directory",
      "path": "/path/to/analysis",
      "repos_path": "/path/to/repos",
      "description": "Existing system analysis",
      "enabled": true
    }
  ]
}
```

### Supported Types

| Type | Description | Status |
|------|-------------|--------|
| `directory` | Local directory with analysis files | Supported |
| `db` | Database-backed knowledge store | Planned |

---

## Command Details

### `/porto gen` - Start Workflow

```bash
/porto gen docs/requirements.md
/porto gen docs/backend.md docs/frontend.md --name "E-Commerce Platform"
```

### `/porto continue` - Next Step

```bash
/porto continue
```

### `/porto resume` - Resume Workflow

```bash
/porto resume a7f3c8b1
```

### `/porto status` - View Status

```bash
/porto status
/porto status a7f3c8b1
/porto status --full
```

### `/porto list` - List Workflows

```bash
/porto list
/porto list --all
/porto list --status in_progress
/porto list --detail
```

---

## Project Structure

```
Porto/
├── install.sh
├── config.example.json
├── README.md
├── agents/
│   ├── en/
│   │   └── prd-analyst.md               # PRD analysis agent
│   └── zhcn/
│       └── prd-analyst.md               # PRD analysis agent (Chinese)
└── skills/
    ├── en/                               # English skills
    │   ├── SKILL.md                      # Skill entry point (subcommand router)
    │   └── references/
    │       ├── gen.md                    # /porto gen command
    │       ├── continue.md               # /porto continue command
    │       ├── resume.md                 # /porto resume command
    │       ├── status.md                 # /porto status command
    │       ├── list.md                   # /porto list command
    │       ├── prd-decomposition.md      # Step 1: Understand
    │       ├── subsystem-identification.md   # Step 2: Identify
    │       ├── subsystem-context-generation.md  # Step 3: Contextualize
    │       ├── subsystem-specification.md    # Step 4: Specify
    │       └── knowledge-retrieval.md    # Knowledge base access helper
    └── zhcn/                             # Chinese skills
        ├── SKILL.md
        └── references/
            └── (same structure as en)
```

### After Installation

```
~/.claude/
├── skills/
│   └── porto/                           # Skill (symlinked)
│       ├── SKILL.md                     # Entry point
│       └── references/                  # All references
└── agents/
    └── prd-analyst.md                   # Agent (symlinked)

~/.porto/
├── config.json                          # Configuration
└── workflows/                           # Workflow data
```

---

## Best Practices

1. **Review each step**: Edit outputs before continuing to next step
2. **Build knowledge base**: Configure knowledge bases for better pattern references
3. **Use clear subsystem names**: Help match with existing systems
4. **Iterate**: Re-run workflows as requirements evolve

---

## Knowledge Base Relationship

| Feature | Knowledge Base | Porto |
|---------|---------------|-------|
| **Purpose** | Store existing codebase analysis | Decompose new requirements |
| **Input** | Git repositories | PRD documents |
| **Output** | Analysis documentation | Subsystem requirement specs |
| **Workflow** | Single run | Interactive 4-step |
| **Relationship** | Generates knowledge base | Consumes knowledge base for patterns |

---

## License

MIT License
