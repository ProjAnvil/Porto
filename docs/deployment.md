# 部署与配置

Porto 的运行形态：`backend/`（uv 管理的 FastAPI + LangGraph agent 服务）+ `frontend/`（Next.js + React + assistant-ui 工作台）。前端默认把 `/api` 代理到后端 `http://127.0.0.1:8100`。

> 默认知识库路径：`~/.scv/analysis`（由 [SCV](https://github.com/ProjAnvil/SCV) 产出） · 默认数据目录：`~/.porto`

---

## 1. 开发模式（前台热重载）

最常用。两个终端分别跑前后端：

```bash
make backend-dev     # 后端：uvicorn --reload，端口 8100，Ctrl-C 停
make frontend-dev    # 前端：next dev，默认端口 3000，Ctrl-C 停
```

或手动：

```bash
# 后端
cd backend
uv sync
cp .env.example .env
uv run uvicorn porto_chatbot.main:app --reload --port 8100

# 前端
cd frontend
npm install
npm run dev
# 如需覆盖后端地址：
# PORTO_API_BASE_URL=http://127.0.0.1:8100 npm run dev
```

也可以用 `make start` 一键在后台同时拉起前后端（带 pid/日志管理，见下文命令速查）。

---

## 2. 配置（`backend/.env`）

后端会自动读取 `backend/.env`。下面是 `backend/.env.example` 的全部项：

```dotenv
# 向量化并检索的知识库路径（SCV 产出）
PORTO_CHATBOT_KB_PATH=~/.scv/analysis

# 本地索引与 workflow 产物目录
PORTO_CHATBOT_DATA_DIR=~/.porto

# RAG 管线。local 用于确定性开发/测试；ollama 用于本地 embedding 模型
PORTO_CHATBOT_EMBEDDING_PROVIDER=local
PORTO_CHATBOT_EMBEDDING_MODEL=qwen3-embedding:0.6b
PORTO_CHATBOT_EMBEDDING_BASE_URL=http://127.0.0.1:11434
PORTO_CHATBOT_MAX_CHUNK_CHARS=1400
PORTO_CHATBOT_CHUNK_OVERLAP=180
PORTO_CHATBOT_TOP_K=6

# Agent LLM 行为。省略 LANGCHAIN_API_KEY 时，后端走确定性本地逻辑（开发/测试）
LANGCHAIN_AGENT_PROVIDER=openai
LANGCHAIN_MODEL=gpt-4.1-mini
LANGCHAIN_BASE_URL=
LANGCHAIN_API_KEY=
LANGCHAIN_TEMPERATURE=0.2
LANGCHAIN_MAX_TOKENS=2000

# PRD 文件解析：hybrid 优先使用模型原生 PDF 视觉能力，失败自动回退本地解析
# local 不把文件发送给模型；native 在模型不支持或调用失败时直接报错
PORTO_CHATBOT_DOCUMENT_PARSE_MODE=hybrid
# 可选 docling；启用前运行：uv sync --extra document-ai
PORTO_CHATBOT_DOCUMENT_LOCAL_PARSER=pypdf
PORTO_CHATBOT_DOCUMENT_MAX_TOKENS=16000
PORTO_CHATBOT_DOCUMENT_MAX_UPLOAD_MB=20
PORTO_CHATBOT_DOCUMENT_MAX_PDF_PAGES=200

# Anthropic 示例：
# LANGCHAIN_AGENT_PROVIDER=anthropic
# LANGCHAIN_MODEL=claude-3-5-sonnet-latest
# LANGCHAIN_BASE_URL=
# LANGCHAIN_API_KEY=...

# 可选：LangGraph 运行链路追踪
# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=...
# LANGSMITH_PROJECT=porto-chatbot
```

> **无 key 也能跑**：每个 LLM 调用都有确定性降级路径。省略 `LANGCHAIN_API_KEY` 即可端到端运行（质量较低，用于开发/测试）。接入模型后，模型是「质量放大器」而非功能开关。详见 [backend-agent-guide.md](backend-agent-guide.md) 的降级哲学一节。

上传 PDF 时，`hybrid` 模式会在 OpenAI/Anthropic 模型支持原生 PDF 输入时读取页面文字和视觉内容；没有 key、模型未知或请求失败时使用 `pypdf` 文本结果。可以通过 `GET /api/porto/document-capabilities` 查看当前静态能力判断。Markdown 的远程图片不会主动下载（防止 SSRF），相对图片需要后续的多附件/资源包上传能力。

需要完全本地的扫描页 OCR、表格与布局恢复时，可运行 `uv sync --extra document-ai` 并设置 `PORTO_CHATBOT_DOCUMENT_LOCAL_PARSER=docling`。Docling 解析仍作为原生视觉模型之前的确定性基线；流程图和原型图的非文字语义建议保留 `hybrid` 模式交给视觉模型补全。

---

## 3. 测试

```bash
cd backend
uv run pytest
```

---

## 4. 向量数据库迁移运维

当用户在 Settings → Vector Database 中保存一个与当前 active profile 存储标识不同的新配置时，后端将其记录为 pending 并返回 `migration_required: true`。前端弹出「立即迁移 / 稍后迁移」模态框。

### 4.1 迁移阶段

迁移在后台线程中执行，全程不阻塞前端。Source profile（通常是 Chroma）在迁移完成前继续正常服务所有读写。

| 阶段 | 说明 |
|------|------|
| `copying` | 批量复制 KB 和 Memory 向量到目标库（每批 500 条） |
| `catching_up` | 增量同步 bulk copy 期间到达的 Memory 记录 |
| `finalizing` | 激活 finalization barrier：阻止新向量写入和 KB 重建提交，执行最后一次 Memory catch-up，确认 KB generation 未变 |
| `verifying` | 在 barrier 下比对 source/target 的 KB 和 Memory 记录数 |
| `activating` | 一个 SQLite 事务内原子切换 active profile + 递增 revision + 清除 pending |

成功后前端自动刷新 active/pending 状态。

### 4.2 稍后迁移

选择「稍后迁移」会关闭模态框，pending profile 保留在 Settings 中并显示琥珀色 Pending 横幅。横幅上有「开始迁移」按钮，随时可以发起迁移。刷新或离开页面不会丢失状态——迁移记录持久化在 SQLite 中。

### 4.3 重试

迁移在 `failed`、`interrupted`、`cancelled` 状态下可以重试。重试利用已持久化的 receipt 和幂等 upsert 从上次中断处恢复，不会重复复制已成功写入的记录。

### 4.4 取消 vs 清理

**取消**（Cancel）只在 `activating` 阶段之前可用。取消只是停止当前工作——已写入目标库的数据不会被删除，source profile 继续服务。

**清理**是独立操作：在 Settings 中删除 pending profile 时触发。系统会删除该 profile 的物理 collection。只有 inactive 的 profile 可以被删除，active profile 无法删除。

### 4.5 Source 保留

迁移成功后，原 source profile 标记为 `inactive` 但其物理 collection 不会被自动删除。这支持以下场景：

- **回滚**：手动将 source profile 重新设为 active（直接修改 `settings.sqlite3` 中的 `vector_store_state` 表）
- **审计**：检查迁移前后数据一致性
- **清理**：确认无误后通过 Settings 界面删除 inactive profile

### 4.6 凭据轮换

修改 profile 的 timeout、token、username 或 password 不需要迁移——这些字段的变更不会改变 storage identity。凭据是 write-only 的：API 响应和日志中永远不会返回凭据值。

### 4.7 中断恢复

进程崩溃或重启时：
1. 启动时自动将残留的 `running` 迁移标记为 `interrupted`
2. interrupted 迁移可以通过「重试」恢复
3. 如果 finalization barrier 被进程崩溃时的定时器释放，下次启动不会遗留 barrier 状态（barrier 是进程内 threading.Event，进程重启后自动清除）

---

## 5. 生产部署

两种打包方式，任选其一。

### 5.1 捆绑部署（单进程 / 单端口，推荐单机一键启动）

前端 `next build --output export` 产出纯静态文件，由后端 FastAPI 同源托管（`/` 返回页面，`/api/*` 走接口），只需一个进程、一个端口。

```bash
make bundle-start          # 本地：构建静态前端 + 启动单一 uvicorn 进程（Ctrl-C 停）
# 或 Docker：
make docker-run-bundled    # 单镜像单容器，端口 8100，挂载 porto-chatbot-data 卷
```

### 5.2 前后端分离部署（各自独立扩缩容 / 发布）

后端 `uvicorn` 与前端 `next start`（自带 Node 服务，通过 rewrites 代理 `/api/*` 到 `PORTO_API_BASE_URL`）分别运行，可部署到不同主机 / 容器。

```bash
make compose-up            # docker compose 启动前后端两个容器
# 或手动：
docker build -t porto-chatbot-backend  -f backend/Dockerfile  backend
docker build -t porto-chatbot-frontend -f frontend/Dockerfile frontend
# 关闭：make compose-down
```

---

## 6. Makefile 命令速查

| 命令 | 作用 |
|------|------|
| `make install` | 安装前后端全部依赖（`uv sync` + `npm install`） |
| `make backend-dev` / `make frontend-dev` | 前台热重载后端 / 前端 |
| `make start` / `make stop` / `make restart` | 后台同时起 / 停 / 重启前后端（带 pid 管理） |
| `make status` / `make logs` / `make logs-follow` | 查看运行状态 / 日志 |
| `make bundle-start` | 捆绑单进程启动（先构建静态前端） |
| `make compose-up` / `make compose-down` | docker compose 起 / 停双容器 |
| `make docker-run-bundled` | 单镜像捆绑容器 |
| `make clean` | 停止所有进程 |
