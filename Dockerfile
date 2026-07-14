# 捆绑部署：单镜像 / 单进程 / 单端口，前端静态导出后由 FastAPI 同源托管。
# 构建上下文须为仓库根目录：
#   docker build -t porto-chatbot -f Dockerfile .
#   docker run -p 8100:8100 -v porto-data:/data --env-file backend/.env porto-chatbot

# ---- Stage 1: 前端静态导出 ----
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build:static

# ---- Stage 2: 后端依赖安装 ----
FROM python:3.12-slim AS backend
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv
WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/src ./src
RUN uv sync --frozen --no-dev

# 前端静态产物拷贝到 settings.static_dir 默认路径 (/app/static)
COPY --from=frontend-builder /app/frontend/out ./static

ENV PATH="/app/.venv/bin:${PATH}" \
    PORTO_CHATBOT_STATIC_DIR=/app/static \
    PORTO_CHATBOT_DATA_DIR=/data \
    PORTO_CHATBOT_LOG_DIR=/data/logs

VOLUME ["/data"]
EXPOSE 8100

CMD ["uvicorn", "porto_chatbot.main:app", "--host", "0.0.0.0", "--port", "8100"]
