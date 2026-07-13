# Porto Chatbot

独立的前后端分离 Porto agent 应用。

## 架构

- `backend/`: uv 管理的 FastAPI + LangGraph agent 服务
- `frontend/`: Next.js + React + assistant-ui 聊天前端
- 默认知识库路径: `~/.scv/analysis`
- 默认数据目录: `~/.porto`

## 后端

```bash
cd chatbot/backend
uv sync
cp .env.example .env
uv run uvicorn porto_chatbot.main:app --reload --port 8100
```

后端会自动读取 `chatbot/backend/.env`。常用配置:

```dotenv
PORTO_CHATBOT_KB_PATH=~/.scv/analysis
PORTO_CHATBOT_DATA_DIR=~/.porto
PORTO_CHATBOT_EMBEDDING_PROVIDER=ollama
PORTO_CHATBOT_EMBEDDING_MODEL=qwen3-embedding:0.6b
PORTO_CHATBOT_EMBEDDING_BASE_URL=http://127.0.0.1:11434
PORTO_CHATBOT_MAX_CHUNK_CHARS=1400
PORTO_CHATBOT_CHUNK_OVERLAP=180
PORTO_CHATBOT_TOP_K=6

LANGCHAIN_AGENT_PROVIDER=openai
LANGCHAIN_MODEL=gpt-4.1-mini
LANGCHAIN_BASE_URL=
LANGCHAIN_API_KEY=
LANGCHAIN_TEMPERATURE=0.2
LANGCHAIN_MAX_TOKENS=2000

# Anthropic example:
# LANGCHAIN_AGENT_PROVIDER=anthropic
# LANGCHAIN_MODEL=claude-3-5-sonnet-latest
# LANGCHAIN_API_KEY=...

# Optional
# LANGSMITH_TRACING=true
# LANGSMITH_API_KEY=...
# LANGSMITH_PROJECT=porto-chatbot
```

## 前端

```bash
cd chatbot/frontend
npm install
npm run dev
```

前端默认代理 `/api` 到 `http://127.0.0.1:8100`。如需覆盖后端地址:

```bash
PORTO_API_BASE_URL=http://127.0.0.1:8100 npm run dev
```

## 测试

```bash
cd chatbot/backend
uv run pytest
```

## 部署

两种打包方式，任选其一：

### 1. 捆绑部署（单进程 / 单端口，推荐用于单机 / 一键启动）

前端 `next build --output export` 产出纯静态文件，由后端 FastAPI 同源托管
（`/` 返回页面，`/api/*` 走接口），只需一个进程、一个端口。

```bash
make bundle-start          # 本地：构建静态前端 + 启动单一 uvicorn 进程
# 或
make docker-run-bundled    # Docker：单镜像单容器
```

### 2. 前后端分离部署（各自独立扩缩容/发布）

后端 `uvicorn` 与前端 `next start`（自带 Node 服务，通过 rewrites 代理 `/api/*`
到 `PORTO_API_BASE_URL`）分别运行，可分别部署到不同主机/容器。

```bash
make compose-up             # docker compose 启动前后端两个容器
# 或手动：
docker build -t porto-chatbot-backend  -f backend/Dockerfile  backend
docker build -t porto-chatbot-frontend -f frontend/Dockerfile frontend
```

日常开发仍用 `make start` / `make backend-dev` / `make frontend-dev`（见上）。

