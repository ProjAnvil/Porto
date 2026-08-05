"""Shared PRD-reading helper for workflow nodes.

Task 7: 节点不再直接读 ``state["prd_text"]`` —— 改为经 ``file_service.read_pages``
分页读取上传文档前 N 页;旧 ``text`` 路由(无 FileService 落盘)按原样回退。

两种 prd 落盘路径共存:
- **upload 路由**: ``prd_file_id`` 是 16-hex 真 file_id。``file_service.get_info`` 命中
  → ``read_pages(1, min(max_pages, page_count))`` 分段读前 N 页。
- **text 路由**(POST /workflows,测试/历史): workflow_executor 的 fallback
  ``prd_file_id = row["prd_file_id"] or row["prd_text"]`` 把原始文本塞进
  ``prd_file_id``;``get_info`` 返回 None → 回退成纯字符串返回(由调用方截断)。

Final review I-1: 当 ``file_service=None`` 且 ``prd_file_id`` 看起来是 16-hex pointer
(而非原始 PRD 文本)时,返回明确错误信息,避免把字面 file_id 串当 PRD 正文喂 LLM。
"""
from __future__ import annotations

import re
from typing import Any

#: 默认读取前 5 页 —— 与 brief 一致(避免一次读长 PRD 触发 6000 字截断)。
_DEFAULT_MAX_PAGES = 5

#: 16-hex file_id 模式(FileService.store 生成: ``uuid.uuid4().hex[:16]``)。
_HEX_POINTER_RE = re.compile(r"^[0-9a-f]{16}$")


def _looks_like_file_pointer(value: str) -> bool:
    """Heuristic: 纯 16-hex 串大概率是 file_id pointer 而非 PRD 原文。"""
    return bool(_HEX_POINTER_RE.match(value))


def read_prd_text(
    state: dict[str, Any],
    file_service: Any = None,
    *,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> str:
    """Read PRD content, preferring FileService pagination over inline text.

    返回原始字符串(可能很长);调用方按需 ``[:2000]`` 或经 ``_truncate`` 截断。
    若两种来源都为空,返回 ``""``。
    """
    prd_file_id = state.get("prd_file_id")
    if prd_file_id and file_service is not None:
        try:
            info = file_service.get_info(prd_file_id)
        except Exception:
            info = None
        if info is not None and info.page_count > 0:
            end = min(max_pages, info.page_count)
            return file_service.read_pages(prd_file_id, 1, end)
    # 回退:text 路由把原始 PRD 存在 prd_file_id(或老字段 prd_text)。
    if prd_file_id:
        # Final review I-1 保险:file_service 未注入但 prd_file_id 是 16-hex pointer
        # → 返回明确错误,而非把字面 id 当 PRD 正文喂 LLM(静默脏数据)。
        if file_service is None and _looks_like_file_pointer(str(prd_file_id)):
            return "PRD 原文需经 file_service 读取，当前未注入"
        return str(prd_file_id)
    return str(state.get("prd_text") or "")
