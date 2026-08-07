# backend/tests/test_file_handlers.py
"""Tests for file-backed tools: get_file_info / read_file_pages / search_file.

These tests exercise the handler layer end-to-end against a real FileService
instance (no mocks): store a document, build an AgentToolContext with
``file_service`` injected, and round-trip through the three handlers defined
in tools/handlers.py.

Registration tests cover both backends:
- build_agent_tools (Langchain ToolDef) — file tools appear only when
  ``ctx.file_service is not None``.
- build_sdk_tools (@tool-decorated) — same gating, skipped when the SDK is
  not importable.
"""
from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from porto_chatbot.files.service import FileService
from porto_chatbot.settings import Settings
from porto_chatbot.tools.context import AgentToolContext
from porto_chatbot.tools.handlers import (
    _read_file_info,
    _read_file_pages,
    _search_file,
)
from porto_chatbot.tools.registry import build_agent_tools


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _make_settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, log_dir=tmp_path / "logs")


def _make_upload(name: str, content: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content))


@pytest.fixture()
def file_service(tmp_path) -> FileService:
    return FileService(_make_settings(tmp_path))


@pytest.fixture()
def stored_file_id(file_service: FileService) -> str:
    """Store a small 2-page text file and return its file_id."""
    # Two distinct keywords so search_file returns targeted hits.
    body = (
        "Project Vision: build a payments platform.\n"
        + "alpha " * 500  # bulk to push it past one virtual page (>2000 chars)
        + "\n Roadmap includes refund flows and risk checks."
        + " beta " * 500
    )
    meta = file_service.store(_make_upload("prd.txt", body.encode("utf-8")), "u1")
    return meta.file_id


@pytest.fixture()
def ctx_with_files(file_service: FileService) -> AgentToolContext:
    return AgentToolContext(state={}, file_service=file_service)


# --------------------------------------------------------------------------- #
# Handler round-trip
# --------------------------------------------------------------------------- #
class TestReadFileInfo:
    def test_returns_metadata_for_stored_file(
        self, ctx_with_files, stored_file_id
    ):
        out = _read_file_info(ctx_with_files, stored_file_id)
        assert "prd.txt" in out
        assert "页数" in out
        assert "大小" in out
        assert "类型" in out
        assert "text/plain" in out

    def test_missing_file_returns_error(self, ctx_with_files):
        out = _read_file_info(ctx_with_files, "no-such-id")
        assert "[错误]" in out
        assert "no-such-id" in out

    def test_raises_when_file_service_missing(self):
        ctx = AgentToolContext(state={}, file_service=None)
        with pytest.raises(RuntimeError, match="file_service"):
            _read_file_info(ctx, "any")


class TestReadFilePages:
    def test_returns_full_text_for_valid_range(
        self, ctx_with_files, stored_file_id
    ):
        # file is at least 2 virtual pages (each >2000 chars)
        info_text = _read_file_info(ctx_with_files, stored_file_id)
        # extract page count from formatted output: "页数: N"
        n_pages = int(info_text.split("页数:")[1].splitlines()[0].strip())
        assert n_pages >= 2
        out = _read_file_pages(ctx_with_files, stored_file_id, 1, n_pages)
        # First page marker and key content always survive (within truncate window).
        assert "第 1 页" in out
        assert "payments" in out
        # Full-range read exceeds _MAX_TOOL_RESULT_CHARS (6000) → truncation marker.
        assert "已截断" in out

    def test_single_page(self, ctx_with_files, stored_file_id):
        out = _read_file_pages(ctx_with_files, stored_file_id, 1, 1)
        assert "第 1 页" in out
        assert "第 2 页" not in out

    def test_invalid_range_surfaces_error_string(
        self, ctx_with_files, stored_file_id
    ):
        out = _read_file_pages(ctx_with_files, stored_file_id, 5, 9)
        assert "[错误]" in out
        assert "页码范围无效" in out

    def test_raises_when_file_service_missing(self):
        ctx = AgentToolContext(state={}, file_service=None)
        with pytest.raises(RuntimeError, match="file_service"):
            _read_file_pages(ctx, "any", 1, 1)


class TestSearchFile:
    def test_finds_known_keyword(self, ctx_with_files, stored_file_id):
        out = _search_file(ctx_with_files, stored_file_id, "Roadmap")
        assert out.startswith("第 ")  # one or more page hits
        assert "Roadmap" in out

    def test_case_insensitive(self, ctx_with_files, stored_file_id):
        # search() lowers both haystack and needle
        out = _search_file(ctx_with_files, stored_file_id, "roadmap")
        assert "Roadmap" in out

    def test_no_hits_returns_not_found_message(
        self, ctx_with_files, stored_file_id
    ):
        out = _search_file(ctx_with_files, stored_file_id, "zzz-not-present")
        assert "未找到" in out
        assert "zzz-not-present" in out

    def test_empty_query_returns_not_found(
        self, ctx_with_files, stored_file_id
    ):
        # FileService.search returns [] for empty query
        out = _search_file(ctx_with_files, stored_file_id, "")
        assert "未找到" in out

    def test_raises_when_file_service_missing(self):
        ctx = AgentToolContext(state={}, file_service=None)
        with pytest.raises(RuntimeError, match="file_service"):
            _search_file(ctx, "any", "q")


# --------------------------------------------------------------------------- #
# build_agent_tools registration (Langchain ToolDef backend)
# --------------------------------------------------------------------------- #
class TestRegistryRegistration:
    def test_no_file_tools_when_file_service_absent(self):
        ctx = AgentToolContext(state={}, file_service=None)
        names = [t.name for t in build_agent_tools(ctx)]
        assert "get_file_info" not in names
        assert "read_file_pages" not in names
        assert "search_file" not in names

    def test_file_tools_registered_when_file_service_present(
        self, ctx_with_files
    ):
        names = [t.name for t in build_agent_tools(ctx_with_files)]
        assert "get_file_info" in names
        assert "read_file_pages" in names
        assert "search_file" in names

    def test_registered_handlers_round_trip_via_tool(
        self, ctx_with_files, stored_file_id
    ):
        tools = {t.name: t for t in build_agent_tools(ctx_with_files)}
        info_out = tools["get_file_info"].handler({"file_id": stored_file_id})
        assert "prd.txt" in info_out
        pages_out = tools["read_file_pages"].handler(
            {"file_id": stored_file_id, "start": 1, "end": 1}
        )
        assert "第 1 页" in pages_out
        search_out = tools["search_file"].handler(
            {"file_id": stored_file_id, "query": "Roadmap"}
        )
        assert "Roadmap" in search_out


# --------------------------------------------------------------------------- #
# build_sdk_tools registration (@tool-decorated backend)
# --------------------------------------------------------------------------- #
def _sdk_available() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _sdk_available(), reason="claude-agent-sdk not installed"
)
class TestSdkRegistration:
    def _build(self, ctx):
        from porto_chatbot.agent_sdk.tools import build_sdk_tools
        return build_sdk_tools(ctx)

    def test_no_file_tools_when_file_service_absent(self):
        ctx = AgentToolContext(state={}, file_service=None)
        tools = self._build(ctx)
        names = [t.name for t in tools]
        assert "get_file_info" not in names
        assert "read_file_pages" not in names
        assert "search_file" not in names

    def test_file_tools_registered_when_file_service_present(
        self, ctx_with_files
    ):
        tools = self._build(ctx_with_files)
        names = [t.name for t in tools]
        assert "get_file_info" in names
        assert "read_file_pages" in names
        assert "search_file" in names

    def test_sdk_file_info_handler_round_trip(
        self, ctx_with_files, stored_file_id
    ):
        import asyncio

        tools = {t.name: t for t in self._build(ctx_with_files)}
        info_tool = tools["get_file_info"]

        async def _call():
            return await info_tool.handler({"file_id": stored_file_id})

        result = asyncio.run(_call())
        text = result["content"][0]["text"]
        assert "prd.txt" in text
