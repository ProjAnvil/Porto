# AI Foundation 本地基础设施设计

**日期：** 2026-08-09  
**状态：** 已确认

## 目标

为 Porto 提供一套可独立启停的本地数据基础设施，包括 PostgreSQL + pgvector、ChromaDB 和 Milvus Standalone。所有对宿主机开放的业务端口使用 `2xxxx` 高位端口，所有持久化数据按服务分类写入 Porto 仓库内的 `data/ai-foundation/`。

SQLite Vector 不在本次范围内。此次工作不修改 Porto 的 Python 向量存储适配代码，也不执行 Git 操作或创建提交。

## 编排方式

在 Porto 根目录新增独立的 `compose.ai-foundation.yml`，Compose 项目名设为 `ai-foundation`。Compose 项目名不支持空格，因此它是用户要求的容器组名称 “AI foundation” 的规范化形式。

基础设施与现有 `docker-compose.yml` 中的 Porto 前后端解耦：开发者可以单独启动、停止或查看基础设施日志，不会隐式构建应用容器。

## 服务与端口

| 服务 | 容器用途 | 宿主端口 | 容器端口 |
|---|---|---:|---:|
| PostgreSQL + pgvector | 关系型数据库和向量扩展 | `25432` | `5432` |
| ChromaDB | 独立向量数据库服务 | `28000` | `8000` |
| Milvus Standalone | 独立向量数据库服务 | `29530` | `19530` |
| etcd | Milvus 内部元数据依赖 | 不暴露 | `2379` |
| MinIO | Milvus 内部对象存储依赖 | 不暴露 | `9000` |

只暴露 Porto 或宿主机客户端直接访问的三个业务端口。Milvus 的内部依赖保留在专用 Compose 网络中，以减少端口冲突和不必要的宿主机入口。

## 数据目录

采用 bind mount，确保数据直接出现在 Porto 本地目录并清晰分类：

```text
data/ai-foundation/
├── postgres/
├── chroma/
└── milvus/
    ├── etcd/
    ├── minio/
    └── data/
```

运行时目录加入 Porto 的 `.gitignore`，防止数据库文件被误纳入版本控制；这只是文件内容变更，不执行任何 Git 命令。

## 配置与安全边界

本地开发所需的数据库名、用户名和密码通过 Compose 环境变量提供，并带有可运行的开发默认值。默认值只面向本机开发，不用于生产环境。镜像使用明确版本，不使用 `latest`，以保证可重复启动。

PostgreSQL 使用包含 pgvector 的镜像，并在首次初始化数据库时执行 `CREATE EXTENSION IF NOT EXISTS vector`。初始化脚本只在空数据目录首次启动时运行。

## 可用性与依赖关系

每个外部服务都配置健康检查。Milvus 等待 etcd 和 MinIO 健康后启动；基础设施容器统一使用 `unless-stopped` 重启策略和专用网络。

启动后应满足：

- PostgreSQL 可通过 `127.0.0.1:25432` 连接，且 `vector` 扩展已启用。
- ChromaDB 健康接口可通过 `127.0.0.1:28000` 访问。
- Milvus SDK 可通过 `127.0.0.1:29530` 建立连接。
- 删除并重建容器不会删除 `data/ai-foundation/` 中的持久化数据。

## 操作入口

Makefile 新增独立命令，用于启动、停止、查看状态和日志。文档记录等价的 `docker compose -f compose.ai-foundation.yml` 命令、端口、默认连接信息、数据目录和彻底清理数据时的注意事项。

停止容器不会删除本地数据；本次实现不提供自动删除数据命令，避免误删。

## 验证

实施完成后执行静态和运行时两层验证：

1. 使用 `docker compose config` 验证 YAML、变量展开、项目名、端口和挂载路径。
2. 启动容器并等待健康检查通过。
3. 查询 PostgreSQL 的 `pg_extension` 确认 pgvector。
4. 调用 ChromaDB 健康接口。
5. 检查 Milvus 健康状态或建立 SDK/HTTP 健康连接。
6. 确认宿主机数据目录已按设计生成。
