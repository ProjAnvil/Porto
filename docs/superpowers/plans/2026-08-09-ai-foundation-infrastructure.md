# AI Foundation Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independently managed local PostgreSQL + pgvector, ChromaDB, and Milvus Standalone stack for Porto with high host ports and categorized bind-mounted data.

**Architecture:** A root-level `compose.ai-foundation.yml` owns the `ai-foundation` Compose project and a private bridge network. PostgreSQL, ChromaDB, and Milvus expose only their public service ports; Milvus uses internal etcd and MinIO dependencies. Runtime state is bind-mounted below `data/ai-foundation/`, while checked-in initialization assets live below `infrastructure/ai-foundation/`.

**Tech Stack:** Docker Compose V2, PostgreSQL 18 + pgvector 0.8.6, ChromaDB 1.5.9, Milvus 2.6.22, etcd 3.5.25, MinIO RELEASE.2024-12-18T13-15-44Z, GNU Make

## Global Constraints

- The Compose project name is exactly `ai-foundation`, the valid normalized form of “AI foundation”.
- Exposed host ports are exactly PostgreSQL `25432`, ChromaDB `28000`, and Milvus `29530`.
- etcd and MinIO are internal-only and have no host port mappings.
- Persistent data uses bind mounts below `./data/ai-foundation/`, categorized by service.
- SQLite Vector is out of scope.
- Do not modify Porto's Python vector-store implementation.
- Do not execute Git commands or create commits.
- Do not provide an automated data deletion target.

---

### Task 1: Add the AI Foundation Compose stack

**Files:**
- Create: `compose.ai-foundation.yml`
- Create: `infrastructure/ai-foundation/postgres/init/001-enable-vector.sql`
- Modify: `.gitignore`

**Interfaces:**
- Produces: Compose services `postgres-vector`, `chromadb`, `milvus-etcd`, `milvus-minio`, and `milvus-standalone` on network `ai-foundation`.
- Produces: host endpoints `postgresql://127.0.0.1:25432`, `http://127.0.0.1:28000`, and `http://127.0.0.1:29530`/Milvus gRPC on `127.0.0.1:29530`.

- [ ] **Step 1: Verify the expected configuration is absent**

Run: `docker compose -f compose.ai-foundation.yml config`

Expected: FAIL because `compose.ai-foundation.yml` does not exist.

- [ ] **Step 2: Create the PostgreSQL initialization script**

Create `infrastructure/ai-foundation/postgres/init/001-enable-vector.sql` containing:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

- [ ] **Step 3: Create the Compose project**

Create `compose.ai-foundation.yml` with:

- `name: ai-foundation`.
- `pgvector/pgvector:0.8.6-pg18-bookworm`, port `25432:5432`, `pg_isready` health check, `./data/ai-foundation/postgres:/var/lib/postgresql` (the PostgreSQL 18+ volume root), and the read-only init directory mount.
- `chromadb/chroma:1.5.9`, port `28000:8000`, a dependency-free `/proc/net/tcp` listener health check (the published image contains no HTTP client), and `./data/ai-foundation/chroma:/data`. Runtime verification still calls `/api/v2/heartbeat` from the host.
- `quay.io/coreos/etcd:v3.5.25` with the official Milvus standalone compaction/quota settings and `./data/ai-foundation/milvus/etcd:/etcd`.
- `minio/minio:RELEASE.2024-12-18T13-15-44Z` with `minio server /minio_data --console-address :9001`, `mc ready local`, and `./data/ai-foundation/milvus/minio:/minio_data`.
- `milvusdb/milvus:v2.6.22` running `milvus run standalone`, with `ETCD_ENDPOINTS`, `MINIO_ADDRESS`, `MQ_TYPE=woodpecker`, port `29530:19530`, `/healthz` health check, and `./data/ai-foundation/milvus/data:/var/lib/milvus`.
- `unless-stopped` for every service and health-gated `depends_on` for Milvus.

Use development defaults with environment overrides:

```yaml
POSTGRES_DB: ${AI_FOUNDATION_POSTGRES_DB:-porto}
POSTGRES_USER: ${AI_FOUNDATION_POSTGRES_USER:-porto}
POSTGRES_PASSWORD: ${AI_FOUNDATION_POSTGRES_PASSWORD:-porto-local-dev}
```

MinIO remains internal-only and uses the matching Milvus standalone defaults `minioadmin` / `minioadmin`.

- [ ] **Step 4: Ignore generated database state**

Add `/data/ai-foundation/` to `.gitignore` without changing existing ignore rules.

- [ ] **Step 5: Validate the rendered Compose model**

Run: `docker compose -f compose.ai-foundation.yml config --quiet`

Expected: exit code 0.

Run: `docker compose -f compose.ai-foundation.yml config`

Expected: project `ai-foundation`; only ports `25432`, `28000`, and `29530` are published; all bind mounts resolve below Porto's `data/ai-foundation`.

### Task 2: Add operation commands and documentation

**Files:**
- Modify: `Makefile`
- Modify: `docs/deployment.md`

**Interfaces:**
- Produces: `make ai-foundation-up`, `make ai-foundation-down`, `make ai-foundation-status`, and `make ai-foundation-logs`.

- [ ] **Step 1: Confirm the operation commands do not exist**

Run: `make -n ai-foundation-status`

Expected: FAIL with “No rule to make target”.

- [ ] **Step 2: Add focused Make targets**

Add the four targets to `.PHONY` and implement them with the exact Compose file:

```make
AI_FOUNDATION_COMPOSE := docker compose -f compose.ai-foundation.yml

ai-foundation-up:
	$(AI_FOUNDATION_COMPOSE) up -d

ai-foundation-down:
	$(AI_FOUNDATION_COMPOSE) down

ai-foundation-status:
	$(AI_FOUNDATION_COMPOSE) ps

ai-foundation-logs:
	$(AI_FOUNDATION_COMPOSE) logs -f
```

Add corresponding help lines without changing the existing application Compose targets.

- [ ] **Step 3: Document local operation**

Add an “AI Foundation 本地数据基建” section to `docs/deployment.md` containing:

- prerequisites: Docker Compose V2 and at least 8 GB Docker VM memory for Milvus;
- start/stop/status/log commands;
- the three host endpoints and development PostgreSQL credentials;
- the `data/ai-foundation/` directory tree;
- the environment variable overrides;
- a warning that `down` preserves bind-mounted data and manual deletion is destructive.

- [ ] **Step 4: Validate Make targets without mutating containers**

Run: `make -n ai-foundation-up ai-foundation-down ai-foundation-status ai-foundation-logs`

Expected: all commands point to `compose.ai-foundation.yml`.

### Task 3: Static and runtime verification

**Files:**
- Modify only the files above if verification exposes an implementation defect.

**Interfaces:**
- Consumes: Task 1 Compose stack and Task 2 operation commands.
- Produces: evidence that each service starts healthy and persists into the required host directory.

- [ ] **Step 1: Validate configuration and resolved mounts**

Run: `docker compose -f compose.ai-foundation.yml config --quiet`

Run: `docker compose -f compose.ai-foundation.yml config | rg '25432|28000|29530|data/ai-foundation'`

Expected: all three high ports and all five categorized data mounts are present.

- [ ] **Step 2: Pull and start the stack**

Run: `docker compose -f compose.ai-foundation.yml up -d --wait`

Expected: PostgreSQL, ChromaDB, etcd, MinIO, and Milvus reach healthy/running state. If Docker is unavailable, report runtime verification as blocked while retaining successful static validation.

- [ ] **Step 3: Verify pgvector**

Run: `docker compose -f compose.ai-foundation.yml exec -T postgres-vector psql -U porto -d porto -Atc "SELECT extversion FROM pg_extension WHERE extname = 'vector';"`

Expected: `0.8.6`.

- [ ] **Step 4: Verify ChromaDB and Milvus health**

Run: `curl --fail --silent http://127.0.0.1:28000/api/v2/heartbeat`

Expected: a successful JSON response.

Run: `docker compose -f compose.ai-foundation.yml exec -T milvus-standalone curl --fail --silent http://localhost:9091/healthz`

Expected: exit code 0.

- [ ] **Step 5: Verify host persistence layout**

Run: `find data/ai-foundation -maxdepth 3 -type d | sort`

Expected: PostgreSQL, ChromaDB, etcd, MinIO, and Milvus data directories exist below `data/ai-foundation/`.

- [ ] **Step 6: Leave the requested foundation available**

Keep the successfully verified stack running. Do not call `down` and do not delete data.
