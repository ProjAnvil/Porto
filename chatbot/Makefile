# Porto Chatbot · 前后端进程管理（pid 文件存 ~/.porto/chatbot/）
BACKEND_PORT  ?= 8100
FRONTEND_PORT ?= 3000
PID_DIR       := $(HOME)/.porto/chatbot
RUN_DIR       := .run
BACKEND_PID   := $(PID_DIR)/backend.pid
FRONTEND_PID  := $(PID_DIR)/frontend.pid
BACKEND_LOG   := $(RUN_DIR)/backend.log
FRONTEND_LOG  := $(RUN_DIR)/frontend.log

.PHONY: help install start stop restart status logs logs-follow backend-start backend-stop frontend-start frontend-stop backend-dev frontend-dev clean \
        bundle-build bundle-start docker-build docker-build-bundled docker-run-bundled compose-up compose-down

help:
	@echo "Porto Chatbot 进程管理（pid 文件: $(PID_DIR)）"
	@echo ""
	@echo "  make install         安装前后端依赖（uv sync + npm install）"
	@echo "  make start           后台启动前后端（分离进程，开发用）"
	@echo "  make stop            停止前后端（读 pid 文件，杀进程树）"
	@echo "  make restart         重启（stop → start）"
	@echo "  make status          查看运行状态"
	@echo "  make logs            打印最近日志"
	@echo "  make logs-follow     实时跟踪日志（Ctrl-C 退出）"
	@echo "  make backend-dev     前台启动后端（--reload 热重载）"
	@echo "  make frontend-dev    前台启动前端"
	@echo "  make clean           停止并清理日志目录"
	@echo ""
	@echo "  make bundle-start    捆绑部署：构建前端静态导出 + 单进程 uvicorn 同源托管前后端"
	@echo "  make compose-up      分离部署：docker compose 启动前后端两个容器"
	@echo "  make docker-run-bundled  捆绑部署：构建并运行单一 Docker 镜像"
	@echo ""
	@echo "端口可用环境变量覆盖：make start FRONTEND_PORT=3001"
	@echo "当前：BACKEND_PORT=$(BACKEND_PORT)  FRONTEND_PORT=$(FRONTEND_PORT)"

install:
	@echo "▶ 安装后端依赖..."
	cd backend && uv sync
	@echo "▶ 安装前端依赖..."
	cd frontend && npm install

# ==================== 后端 ====================

backend-start:
	@mkdir -p $(PID_DIR) $(RUN_DIR)
	@if [ -f $(BACKEND_PID) ] && kill -0 $$(cat $(BACKEND_PID)) 2>/dev/null; then \
	    echo "⚠️  后端已在运行 (pid $$(cat $(BACKEND_PID)))"; \
	else \
	    echo "▶ 启动后端 http://localhost:$(BACKEND_PORT) ..."; \
	    sh -c 'cd backend && exec uv run uvicorn porto_chatbot.main:app --host 127.0.0.1 --port $(BACKEND_PORT)' > $(BACKEND_LOG) 2>&1 & \
	    echo $$! > $(BACKEND_PID); \
	    sleep 1; \
	    echo "   pid=$$(cat $(BACKEND_PID))  日志: $(BACKEND_LOG)"; \
	fi

backend-stop:
	@pid=$$(cat $(BACKEND_PID) 2>/dev/null); \
	if [ -z "$$pid" ]; then echo "后端未运行（无 pid 文件）"; \
	elif ! kill -0 $$pid 2>/dev/null; then echo "后端未运行（pid $$pid 已退出）"; rm -f $(BACKEND_PID); \
	else \
	    for c in $$(pgrep -P $$pid); do kill -TERM $$c 2>/dev/null || true; done; \
	    kill -TERM $$pid 2>/dev/null || true; \
	    sleep 1; kill -9 $$pid 2>/dev/null || true; \
	    rm -f $(BACKEND_PID); \
	    echo "⏹  后端已停止 (pid $$pid)"; \
	fi

# ==================== 前端 ====================

frontend-start:
	@mkdir -p $(PID_DIR) $(RUN_DIR)
	@if [ -f $(FRONTEND_PID) ] && kill -0 $$(cat $(FRONTEND_PID)) 2>/dev/null; then \
	    echo "⚠️  前端已在运行 (pid $$(cat $(FRONTEND_PID)))"; \
	else \
	    echo "▶ 启动前端 http://localhost:$(FRONTEND_PORT) ..."; \
	    sh -c 'cd frontend && PORTO_API_BASE_URL=http://127.0.0.1:$(BACKEND_PORT) exec npm run dev -- -p $(FRONTEND_PORT)' > $(FRONTEND_LOG) 2>&1 & \
	    echo $$! > $(FRONTEND_PID); \
	    sleep 1; \
	    echo "   pid=$$(cat $(FRONTEND_PID))  日志: $(FRONTEND_LOG)"; \
	fi

frontend-stop:
	@pid=$$(cat $(FRONTEND_PID) 2>/dev/null); \
	if [ -z "$$pid" ]; then echo "前端未运行（无 pid 文件）"; \
	elif ! kill -0 $$pid 2>/dev/null; then echo "前端未运行（pid $$pid 已退出）"; rm -f $(FRONTEND_PID); \
	else \
	    for c in $$(pgrep -P $$pid); do kill -TERM $$c 2>/dev/null || true; done; \
	    kill -TERM $$pid 2>/dev/null || true; \
	    sleep 1; kill -9 $$pid 2>/dev/null || true; \
	    rm -f $(FRONTEND_PID); \
	    echo "⏹  前端已停止 (pid $$pid)"; \
	fi

# ==================== 组合 ====================

start: backend-start frontend-start
	@sleep 3
	@echo ""
	@echo "✅ 启动指令已发出（前端首次编译 + 后端冷启动可能要几秒）"
	@echo "   前端:  http://localhost:$(FRONTEND_PORT)"
	@echo "   后端:  http://localhost:$(BACKEND_PORT)/api/health"
	@echo "   make status | make logs | make stop"

stop: backend-stop frontend-stop

restart:
	@$(MAKE) --no-print-directory stop
	@sleep 2
	@$(MAKE) --no-print-directory start

status:
	@if [ -f $(BACKEND_PID) ] && kill -0 $$(cat $(BACKEND_PID)) 2>/dev/null; then echo "✅ 后端运行中 (pid $$(cat $(BACKEND_PID)), :$(BACKEND_PORT))"; else echo "❌ 后端未运行"; fi
	@if [ -f $(FRONTEND_PID) ] && kill -0 $$(cat $(FRONTEND_PID)) 2>/dev/null; then echo "✅ 前端运行中 (pid $$(cat $(FRONTEND_PID)), :$(FRONTEND_PORT))"; else echo "❌ 前端未运行"; fi

logs:
	@echo "=== 后端（最近 40 行）==="
	@tail -n 40 $(BACKEND_LOG) 2>/dev/null || echo "（无日志）"
	@echo ""
	@echo "=== 前端（最近 40 行）==="
	@tail -n 40 $(FRONTEND_LOG) 2>/dev/null || echo "（无日志）"

logs-follow:
	@echo "跟踪日志（Ctrl-C 退出）..."
	@tail -f $(BACKEND_LOG) $(FRONTEND_LOG)

# ==================== 前台开发（热重载）====================

backend-dev:
	@echo "▶ 前台启动后端（--reload，Ctrl-C 停）..."
	@cd backend && exec uv run uvicorn porto_chatbot.main:app --reload --host 127.0.0.1 --port $(BACKEND_PORT)

frontend-dev:
	@echo "▶ 前台启动前端（Ctrl-C 停）..."
	@cd frontend && PORTO_API_BASE_URL=http://127.0.0.1:$(BACKEND_PORT) exec npm run dev -- -p $(FRONTEND_PORT)

clean: stop
	@rm -rf $(RUN_DIR)
	@echo "🧹 已清理 $(RUN_DIR)"

# ==================== 捆绑部署（单进程，前端静态导出由后端同源托管）====================
# 一次构建后，只需一个 uvicorn 进程即可同时提供前端页面和 /api 接口。

bundle-build:
	@echo "▶ 构建前端静态导出..."
	cd frontend && npm install && npm run build:static
	@echo "▶ 拷贝静态产物到 backend/static ..."
	rm -rf backend/static
	cp -r frontend/out backend/static
	@echo "✅ 捆绑产物就绪: backend/static"

bundle-start: bundle-build
	@echo "▶ 启动捆绑进程 http://localhost:$(BACKEND_PORT) （前端+后端同源，Ctrl-C 停）..."
	cd backend && exec uv run uvicorn porto_chatbot.main:app --host 0.0.0.0 --port $(BACKEND_PORT)

# ==================== Docker ====================

docker-build:
	docker compose build

compose-up:
	docker compose up -d --build
	@echo "✅ 前后端分离容器已启动"
	@echo "   前端: http://localhost:3000  后端: http://localhost:8100/api/health"

compose-down:
	docker compose down

docker-build-bundled:
	docker build -t porto-chatbot -f Dockerfile .

docker-run-bundled: docker-build-bundled
	docker run --rm -p $(BACKEND_PORT):8100 \
	  --env-file backend/.env \
	  -v porto-chatbot-data:/data \
	  porto-chatbot
