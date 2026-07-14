# 后端独立镜像（前后端分离部署）
# 仅运行 FastAPI/uvicorn，不包含前端。构建上下文须为 backend/ 目录：
#   docker build -t porto-chatbot-backend -f backend/Dockerfile backend
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    PORTO_CHATBOT_DATA_DIR=/data \
    PORTO_CHATBOT_LOG_DIR=/data/logs

VOLUME ["/data"]
EXPOSE 8100

CMD ["uvicorn", "porto_chatbot.main:app", "--host", "0.0.0.0", "--port", "8100"]
