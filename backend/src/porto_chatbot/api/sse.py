from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from ..llm.types import ContentType
from ..models import ChatRequest
from ..models.enums import ChatRole


def _ai_sdk_sse(part: dict[str, Any]) -> str:
    return f"data: {json.dumps(part, ensure_ascii=False)}\n\n"


def _text_chunks(text: str, chunk_size: int = 48) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _extract_text_from_ai_sdk_part(part: dict[str, Any]) -> str:
    if part.get("type") == ContentType.TEXT:
        return str(part.get("text") or "")
    return ""


def _extract_latest_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != ChatRole.USER:
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        parts = message.get("parts")
        if isinstance(parts, list):
            return "\n".join(
                _extract_text_from_ai_sdk_part(part)
                for part in parts
                if isinstance(part, dict)
            ).strip()
    return ""


def _chat_request_from_stream_body(body: dict[str, Any]) -> ChatRequest:
    if "message" in body:
        return ChatRequest.model_validate(body)
    message = _extract_latest_user_message(body.get("messages") or [])
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    return ChatRequest(
        message=message,
        session_id=body.get("session_id") or body.get("sessionId") or body.get("id") or "default",
        top_k=body.get("top_k"),
        rag=body.get("rag"),
        agent=body.get("agent"),
    )
