# PRD 文件理解与多模态解析 实施计划

> 按 superpowers 的小步验证方式执行；每个任务先补失败测试，再实现并跑相关测试。

**Goal:** 上传 PDF/Markdown/TXT PRD 时保留可提取文本，并在模型支持时理解 PDF 图片、图表与页面布局，所有模型失败均可审计降级。

## Task 1：统一解析模型与本地解析

- [x] 新增 `DocumentArtifact`、`DocumentImageRef`、解析异常。
- [x] Markdown 检测图片引用；PDF 统计页面与图片并保留文本。
- [x] 保持 `read_document()` 向后兼容。
- [x] 增加文件大小、PDF 页数校验测试。

## Task 2：模型文件能力和 provider payload

- [x] 新增 `ModelCapabilities` 与 capability 判断。
- [x] 新增 `complete_document()`。
- [x] 覆盖 OpenAI 与 Anthropic Base64 PDF payload 测试。
- [x] 未启用或不支持时不发请求。

## Task 3：混合解析服务

- [x] 新增本地/native/hybrid 三种模式。
- [x] hybrid 原生失败回退并记录 warning。
- [x] native 模式失败时明确报错。
- [x] 仅接受非空模型结果。

## Task 4：上传接口接入和错误映射

- [x] 上传接口先校验大小和类型。
- [x] 使用生效 agent settings 构造 LLMClient。
- [x] 将解析错误映射为 400/413/415/422。
- [x] 增加 API 回归测试。

## Task 5：配置、文档和全量验证

- [x] Settings 增加解析模式、local parser 与限制。
- [x] `.env.example` 和部署文档说明配置与 Docling 可选增强。
- [x] 运行 backend pytest、ruff。
