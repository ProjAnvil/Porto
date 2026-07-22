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

## 4. 生产部署

两种打包方式，任选其一。

### 4.1 捆绑部署（单进程 / 单端口，推荐单机一键启动）

前端 `next build --output export` 产出纯静态文件，由后端 FastAPI 同源托管（`/` 返回页面，`/api/*` 走接口），只需一个进程、一个端口。

```bash
make bundle-start          # 本地：构建静态前端 + 启动单一 uvicorn 进程（Ctrl-C 停）
# 或 Docker：
make docker-run-bundled    # 单镜像单容器，端口 8100，挂载 porto-chatbot-data 卷
```

### 4.2 前后端分离部署（各自独立扩缩容 / 发布）

后端 `uvicorn` 与前端 `next start`（自带 Node 服务，通过 rewrites 代理 `/api/*` 到 `PORTO_API_BASE_URL`）分别运行，可部署到不同主机 / 容器。

```bash
make compose-up            # docker compose 启动前后端两个容器
# 或手动：
docker build -t porto-chatbot-backend  -f backend/Dockerfile  backend
docker build -t porto-chatbot-frontend -f frontend/Dockerfile frontend
# 关闭：make compose-down
```

---

## 5. Makefile 命令速查

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
