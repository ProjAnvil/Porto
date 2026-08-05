"""Shared PRD-reading helper for workflow nodes.

Task 7: 节点不再直接读 ``state["prd_text"]`` —— 改为经 ``file_service.read_pages``
分页读取上传文档前 N 页;旧 ``text`` 路由(无 FileService 落盘)按原样回退。

两种 prd 落盘路径共存:
- **upload 路由**: ``prd_file_id`` 是 16-hex 真 file_id。``file_service.get_info`` 命中
  → ``read_pages(1, min(max_pages, page_count))`` 分段读前 N 页。
- **text 路由**(POST /workflows,测试/历史): workflow_executor 的 fallback
  ``prd_file_id = row["prd_file_id"] or row["prd_text"]`` 把原始文本塞进
  ``prd_file_id``;``get_info`` 返回 None → 回退成纯字符串返回(由调用方截断)。
"""
from __future__ import annotations

from typing import Any

#: 默认读取前 5 页 —— 与 brief 一致(避免一次读长 PRD 触发 6000 字截断)。
_DEFAULT_MAX_PAGES = 5


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
        return str(prd_file_id)
    return str(state.get("prd_text") or "")
