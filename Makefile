.PHONY: test test-server test-workflow test-verbose serve install clean help

help: ## 显示帮助信息
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

test: ## 运行所有测试
	uv run pytest

test-verbose: ## 运行所有测试（详细输出）
	uv run pytest -v

test-server: ## 仅运行 server 测试
	uv run pytest tests/test_porto_server.py -v

test-workflow: ## 仅运行 workflow 测试
	uv run pytest tests/test_porto_workflow.py -v

serve: ## 启动开发服务器（使用测试 seed 数据）
	uv run python porto_server.py --porto-home tests/porto_home --port 8090

install: ## 安装依赖
	uv sync

clean: ## 清理缓存文件
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
