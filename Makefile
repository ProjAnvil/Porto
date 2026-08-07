# Porto Chatbot · frontend/backend process management (pid files in ~/.porto/agent/)
BACKEND_PORT  ?= 8100
FRONTEND_PORT ?= 3100
PID_DIR       := $(HOME)/.porto/chatbot
RUN_DIR       := .run
BACKEND_PID   := $(PID_DIR)/backend.pid
FRONTEND_PID  := $(PID_DIR)/frontend.pid
BACKEND_LOG   := $(RUN_DIR)/backend.log
FRONTEND_LOG  := $(RUN_DIR)/frontend.log

.PHONY: help install start stop restart status logs logs-follow backend-start backend-stop frontend-start frontend-stop backend-dev frontend-dev clean \
        bundle-build bundle-start docker-build docker-build-bundled docker-run-bundled compose-up compose-down eval-install eval-dataset eval-test eval-run eval-sweep

help:
	@echo "Porto Chatbot process manager (pid dir: $(PID_DIR))"
	@echo ""
	@echo "  make install         Install frontend & backend deps (uv sync + npm install)"
	@echo "  make start           Start frontend & backend in background (detached, for dev)"
	@echo "  make stop            Stop frontend & backend (read pid files, kill process trees)"
	@echo "  make restart         Restart (stop -> start)"
	@echo "  make status          Show running status"
	@echo "  make logs            Print recent logs"
	@echo "  make logs-follow     Tail logs in real time (Ctrl-C to exit)"
	@echo "  make backend-dev     Run backend in foreground (--reload hot reload)"
	@echo "  make frontend-dev    Run frontend in foreground"
	@echo "  make clean           Stop and clean the log directory"
	@echo ""
	@echo "  make bundle-start    Bundled deploy: build static frontend + serve both via one uvicorn (same origin)"
	@echo "  make compose-up      Split deploy: docker compose up frontend & backend in two containers"
	@echo "  make docker-run-bundled  Bundled deploy: build & run a single Docker image"
	@echo ""
	@echo "Override ports via env: make start FRONTEND_PORT=3001"
	@echo "Current: BACKEND_PORT=$(BACKEND_PORT)  FRONTEND_PORT=$(FRONTEND_PORT)"

install:
	@echo "▶ Installing backend deps..."
	cd backend && uv sync
	@echo "▶ Installing frontend deps..."
	cd frontend && npm install

# ==================== Backend ====================

backend-start:
	@mkdir -p $(PID_DIR) $(RUN_DIR)
	@if [ -f $(BACKEND_PID) ] && kill -0 $$(cat $(BACKEND_PID)) 2>/dev/null; then \
	    echo "⚠️  Backend already running (pid $$(cat $(BACKEND_PID)))"; \
	else \
	    echo "▶ Starting backend at http://localhost:$(BACKEND_PORT) ..."; \
	    sh -c 'cd backend && exec uv run uvicorn porto_chatbot.main:app --host 127.0.0.1 --port $(BACKEND_PORT)' > $(BACKEND_LOG) 2>&1 & \
	    echo $$! > $(BACKEND_PID); \
	    sleep 1; \
	    echo "   pid=$$(cat $(BACKEND_PID))  log: $(BACKEND_LOG)"; \
	fi

backend-stop:
	@pid=$$(cat $(BACKEND_PID) 2>/dev/null); \
	if [ -z "$$pid" ]; then echo "Backend not running (no pid file)"; \
	elif ! kill -0 $$pid 2>/dev/null; then echo "Backend not running (pid $$pid exited)"; rm -f $(BACKEND_PID); \
	else \
	    for c in $$(pgrep -P $$pid); do kill -TERM $$c 2>/dev/null || true; done; \
	    kill -TERM $$pid 2>/dev/null || true; \
	    sleep 1; kill -9 $$pid 2>/dev/null || true; \
	    rm -f $(BACKEND_PID); \
	    echo "⏹  Backend stopped (pid $$pid)"; \
	fi

# ==================== Frontend ====================

frontend-start:
	@mkdir -p $(PID_DIR) $(RUN_DIR)
	@if [ -f $(FRONTEND_PID) ] && kill -0 $$(cat $(FRONTEND_PID)) 2>/dev/null; then \
	    echo "⚠️  Frontend already running (pid $$(cat $(FRONTEND_PID)))"; \
	else \
	    echo "▶ Starting frontend at http://localhost:$(FRONTEND_PORT) ..."; \
	    sh -c 'cd frontend && PORTO_API_BASE_URL=http://127.0.0.1:$(BACKEND_PORT) exec npm run dev -- -p $(FRONTEND_PORT)' > $(FRONTEND_LOG) 2>&1 & \
	    echo $$! > $(FRONTEND_PID); \
	    sleep 1; \
	    echo "   pid=$$(cat $(FRONTEND_PID))  log: $(FRONTEND_LOG)"; \
	fi

frontend-stop:
	@pid=$$(cat $(FRONTEND_PID) 2>/dev/null); \
	if [ -z "$$pid" ]; then echo "Frontend not running (no pid file)"; \
	elif ! kill -0 $$pid 2>/dev/null; then echo "Frontend not running (pid $$pid exited)"; rm -f $(FRONTEND_PID); \
	else \
	    for c in $$(pgrep -P $$pid); do kill -TERM $$c 2>/dev/null || true; done; \
	    kill -TERM $$pid 2>/dev/null || true; \
	    sleep 1; kill -9 $$pid 2>/dev/null || true; \
	    rm -f $(FRONTEND_PID); \
	    echo "⏹  Frontend stopped (pid $$pid)"; \
	fi

# ==================== Combined ====================

start: backend-start frontend-start
	@sleep 3
	@echo ""
	@echo "✅ Start commands issued (frontend first build + backend cold start may take a few seconds)"
	@echo "   Frontend: http://localhost:$(FRONTEND_PORT)"
	@echo "   Backend:  http://localhost:$(BACKEND_PORT)/api/health"
	@echo "   make status | make logs | make stop"

stop: backend-stop frontend-stop

restart:
	@$(MAKE) --no-print-directory stop
	@sleep 2
	@$(MAKE) --no-print-directory start

status:
	@if [ -f $(BACKEND_PID) ] && kill -0 $$(cat $(BACKEND_PID)) 2>/dev/null; then echo "✅ Backend running (pid $$(cat $(BACKEND_PID)), :$(BACKEND_PORT))"; else echo "❌ Backend not running"; fi
	@if [ -f $(FRONTEND_PID) ] && kill -0 $$(cat $(FRONTEND_PID)) 2>/dev/null; then echo "✅ Frontend running (pid $$(cat $(FRONTEND_PID)), :$(FRONTEND_PORT))"; else echo "❌ Frontend not running"; fi

logs:
	@echo "=== Backend (last 40 lines) ==="
	@tail -n 40 $(BACKEND_LOG) 2>/dev/null || echo "(no log)"
	@echo ""
	@echo "=== Frontend (last 40 lines) ==="
	@tail -n 40 $(FRONTEND_LOG) 2>/dev/null || echo "(no log)"

logs-follow:
	@echo "Tailing logs (Ctrl-C to exit)..."
	@tail -f $(BACKEND_LOG) $(FRONTEND_LOG)

# ==================== Foreground dev (hot reload) ====================

backend-dev:
	@echo "▶ Starting backend in foreground (--reload, Ctrl-C to stop)..."
	@cd backend && exec uv run uvicorn porto_chatbot.main:app --reload --host 127.0.0.1 --port $(BACKEND_PORT)

frontend-dev:
	@echo "▶ Starting frontend in foreground (Ctrl-C to stop)..."
	@cd frontend && PORTO_API_BASE_URL=http://127.0.0.1:$(BACKEND_PORT) exec npm run dev -- -p $(FRONTEND_PORT)

clean: stop
	@rm -rf $(RUN_DIR)
	@echo "🧹 Cleaned $(RUN_DIR)"

# ==================== Bundled deploy (single process; static frontend served by backend, same origin) ====================
# After a one-shot build, a single uvicorn process serves both frontend pages and /api endpoints.

bundle-build:
	@echo "▶ Building static frontend export..."
	cd frontend && npm install && npm run build:static
	@echo "▶ Copying static assets to backend/static ..."
	rm -rf backend/static
	cp -r frontend/out backend/static
	@echo "✅ Bundle ready: backend/static"

bundle-start: bundle-build
	@echo "▶ Starting bundled server at http://localhost:$(BACKEND_PORT) (frontend+backend same origin, Ctrl-C to stop)..."
	cd backend && exec uv run uvicorn porto_chatbot.main:app --host 0.0.0.0 --port $(BACKEND_PORT)

# ==================== Docker ====================

docker-build:
	docker compose build

compose-up:
	docker compose up -d --build
	@echo "✅ Frontend & backend containers started (split deploy)"
	@echo "   Frontend: http://localhost:3000  Backend: http://localhost:8100/api/health"

compose-down:
	docker compose down

docker-build-bundled:
	docker build -t porto-chatbot -f Dockerfile .

docker-run-bundled: docker-build-bundled
	docker run --rm -p $(BACKEND_PORT):8100 \
	  --env-file backend/.env \
	  -v porto-chatbot-data:/data \
	  porto-chatbot

# ==================== RAG eval (DeepEval) ====================

eval-install: ## 安装 RAG 评测可选依赖 (deepeval, gdown)
	cd backend && pip install -e ".[eval]"

eval-dataset: ## 下载 DomainRAG 评测数据集到 gitignored 目录
	cd backend && python -m tests.rag_eval.scripts.fetch_dataset

eval-test: ## 运行 DeepEval RAG 质量门禁 (需 LLM key + 数据集)
	cd backend && pytest -m integration tests/rag_eval/test_rag_gate.py

eval-run: ## 运行 RAG 检索实验 (PROFILE=name，默认列出所有 profile)
	cd backend && python -m tests.rag_eval.experiment $(or $(PROFILE),--list)

eval-sweep: ## 批量跑多个 profile 对比 (默认全扫；耗时很长)
	cd backend && python -m tests.rag_eval.experiment --sweep-all
