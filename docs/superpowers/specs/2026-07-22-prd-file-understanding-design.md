# PRD 文件理解与多模态解析

**日期**：2026-07-22  
**状态**：已确认，实施中  
**涉及**：`backend/src/porto_chatbot/documents.py`、`backend/src/porto_chatbot/llm/`、workflow 上传接口

## 1. 目标

让 Porto 上传的 PDF、Markdown、TXT PRD 在进入既有 workflow 前，先被转换成可审计的规范化文本；PDF 中的扫描页、流程图、原型图和表格在模型支持时通过原生文件视觉能力补全。本地解析、模型能力未知或模型调用失败时必须可降级，不能阻断现有纯文本流程。

## 2. 当前问题

- 上传接口把文件写入临时路径后调用 `read_document()`，最终只保存字符串。
- PDF 使用 `pypdf.extract_text()`，扫描页和视觉内容丢失。
- Markdown/TXT 直接读取；Markdown 图片引用没有被检查，相对资源在临时文件删除后不可恢复。
- `LLMClient` 只构造纯文本消息，没有 provider-specific 文件内容块。
- 仅从模型名称无法可靠判断任意 OpenAI-compatible 服务是否支持文件输入。

## 3. 决策

### 3.1 混合解析

```
upload bytes
  -> 安全校验（扩展名、大小、PDF 页数）
  -> 本地确定性解析 DocumentArtifact
  -> PDF + native capability + hybrid/native 模式
       -> provider 原生 PDF 理解
       -> 成功：以模型生成的 faithful Markdown 为正文
       -> 失败：保留本地正文并记录 warning
  -> workflow_store.prd_text
```

原生视觉理解只在上传时执行一次。下游 understand/identify/generate 继续消费规范化文本，避免在每个节点重复上传 PDF、重复计费。

### 3.2 能力检测

新增 `ModelCapabilities`，区分：

- client 是否启用；
- 是否支持图片输入；
- 是否支持原生 PDF；
- 判断依据与置信度。

OpenAI 官方模型按已知视觉模型族判断；Anthropic 当前模型默认支持 PDF。自定义 base URL 或未知模型标记为未知，不因静态判断阻断本地解析。真正调用仍以请求成功为准，错误自动降级。

### 3.3 统一解析结果

`DocumentArtifact` 包含：

- `text`：进入 workflow 的规范化 Markdown/文本；
- `format`、`parser`、`page_count`；
- `image_refs`：Markdown 图片引用或 PDF 图片统计；
- `warnings`；
- `used_native_vision`。

保留现有 `read_document(path) -> str` 作为知识库索引兼容入口。

### 3.4 Provider 适配

- OpenAI：Chat Completions 文件 content block（Base64 data URL），与项目现有 API 保持一致。
- Anthropic：Messages API `document` block，Base64 `application/pdf`。
- 原生结果的 prompt 要求保留标题、列表、表格、约束，并描述图表/原型中的产品语义，不凭空补需求。

### 3.5 本地增强后端

第一阶段保留 `pypdf` 快速路径，并把图片缺失显式记录为 warning。Docling 作为可选增强后端：安装后可导出结构化 Markdown；未安装时无影响。这样不强制所有 Porto 部署承担 ML 模型依赖和镜像体积。

## 4. 配置

- `document_parse_mode`: `local | native | hybrid`，默认 `hybrid`。
- `document_max_upload_mb`: 默认 20 MB。
- `document_max_pdf_pages`: 默认 200 页。

`local` 从不调用模型；`native` 要求原生文件能力，失败返回明确错误；`hybrid` 优先原生、失败回退本地。

## 5. 安全与边界

- 扩展名必须在支持白名单中，未知类型返回 415。
- 上传体积和 PDF 页数超限返回 413。
- 加密、损坏或不可解析文件返回 400，而不是 500。
- 第一阶段不抓取 Markdown 远程图片，避免 SSRF；只记录引用和 warning。
- 单独上传 Markdown 无法解析相对图片；未来以 ZIP/多附件 manifest 解决。
- TXT 本身没有内嵌图片能力。

## 6. 验证

- documents 单元测试：PDF/Markdown 图片检测、大小/页数、空文档、降级。
- LLM 单元测试：OpenAI/Anthropic 文件 payload 与 capability。
- API 测试：不支持扩展名、超限、解析异常映射、无 key 本地回退。
- 完整后端 pytest + ruff。

