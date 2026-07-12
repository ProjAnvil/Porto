from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from openai import OpenAI

from .logging_utils import get_component_logger
from .models import SourceChunk
from .settings import Settings

Message = dict[str, Any]


@dataclass
class ToolDef:
    """单个工具定义：schema 给 LLM，handler 在本地执行。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: str


@dataclass
class ToolLoopResult:
    """complete_with_tools 的返回。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    truncated: bool = False


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def _try_parse_json(text: str) -> dict | None:
    """容忍 LLM 把 JSON 包在 fence 或夹杂文本里。"""
    if not text:
        return None
    candidates = [text]
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1))
    bare = _BARE_JSON_RE.search(text)
    if bare:
        candidates.insert(0, bare.group(1))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            continue
    return None


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("llm", settings)
        self._client = self._build_client()
        self.logger.info(
            "llm client ready enabled=%s provider=%s model=%s base_url_set=%s",
            self.enabled,
            self.settings.agent_provider,
            self.settings.agent_model,
            bool(self.settings.agent_base_url),
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------ #
    # 基础补全：兼容旧 complete(system, user)，新增 messages 多轮
    # ------------------------------------------------------------------ #
    def complete(self, system: str, user: str, *, messages: list[Message] | None = None) -> str | None:
        if self._client is None:
            self.logger.info("llm complete skipped disabled")
            return None
        msgs = self._normalize_messages(system, user, messages)
        self.logger.info(
            "llm complete start provider=%s model=%s messages=%s",
            self.settings.agent_provider,
            self.settings.agent_model,
            len(msgs),
        )
        if self.settings.agent_provider == "openai":
            content = self._openai_text(msgs)
        elif self.settings.agent_provider == "anthropic":
            content = self._anthropic_text(msgs)
        else:
            raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")
        self.logger.info("llm complete finish answer_chars=%s", len(content))
        return content

    # ------------------------------------------------------------------ #
    # 结构化输出：返回 dict（解析失败重试一次，再失败返回 None 交由调用方降级）
    # ------------------------------------------------------------------ #
    def complete_structured(
        self,
        system: str,
        user: str,
        schema_hint: dict[str, Any],
        *,
        messages: list[Message] | None = None,
    ) -> dict | None:
        if self._client is None:
            self.logger.info("llm complete_structured skipped disabled")
            return None
        schema_text = json.dumps(schema_hint, ensure_ascii=False)
        enriched_system = (
            f"{system}\n\n必须严格输出符合下面 JSON Schema 的纯 JSON 对象，"
            f"不要任何额外说明文字、不要 markdown 代码围栏：\n{schema_text}"
        )
        raw = self.complete(enriched_system, user, messages=messages)
        parsed = _try_parse_json(raw or "")
        if parsed is not None:
            self.logger.info("llm complete_structured finish parsed=true keys=%s", len(parsed))
            return parsed
        self.logger.warning("llm complete_structured parse failed retrying raw_chars=%s", len(raw or ""))
        retry_user = f"{user}\n\n你上次的输出无法解析为 JSON，请只输出合法的 JSON 对象。"
        raw2 = self.complete(enriched_system, retry_user)
        parsed2 = _try_parse_json(raw2 or "")
        self.logger.info("llm complete_structured retry parsed=%s", parsed2 is not None)
        return parsed2

    # ------------------------------------------------------------------ #
    # Tool calling loop：LLM 自主选工具、本地执行、回填，直到无 tool_call 或 max_turns
    # ------------------------------------------------------------------ #
    def complete_with_tools(
        self,
        system: str,
        user: str,
        tools: list[ToolDef],
        *,
        messages: list[Message] | None = None,
        max_turns: int | None = None,
    ) -> ToolLoopResult:
        if self._client is None:
            self.logger.info("llm complete_with_tools skipped disabled")
            return ToolLoopResult(text="")
        if not tools:
            return ToolLoopResult(text=self.complete(system, user, messages=messages) or "")
        resolved_turns = max_turns or self.settings.agent_max_tool_turns
        handlers = {t.name: t for t in tools}
        result = ToolLoopResult()

        # 初始 messages（openai 风格，含 system）
        convo: list[Message] = self._normalize_messages(system, user, messages)

        for turn in range(1, resolved_turns + 1):
            result.turns = turn
            tool_calls, assistant_text = self._provider_tool_step(convo, tools)
            if not tool_calls:
                result.text = assistant_text
                self.logger.info(
                    "llm tool loop stop reason=no_tool_calls turns=%s tool_calls_total=%s",
                    turn,
                    len(result.tool_calls),
                )
                return result

            # 把 assistant 这一步（含 tool_call 请求）追加到对话
            self._append_assistant_tool_step(convo, assistant_text, tool_calls)
            # 执行每个 tool_call 并回填结果
            for call in tool_calls:
                tool_name = call["name"]
                tool_args = call["arguments"]
                tool_def = handlers.get(tool_name)
                if tool_def is None:
                    outcome = f"错误：未知工具 {tool_name}"
                else:
                    try:
                        outcome = tool_def.handler(tool_args)
                    except Exception as exc:  # noqa: BLE001 — 工具失败不应打断 loop
                        self.logger.exception("llm tool handler failed name=%s", tool_name)
                        outcome = f"错误：工具 {tool_name} 执行失败：{exc}"
                result.tool_calls.append(
                    ToolCall(name=tool_name, arguments=tool_args, result=outcome)
                )
                self._append_tool_result(convo, call, outcome)
                self.logger.info(
                    "llm tool call name=%s args_keys=%s result_chars=%s",
                    tool_name,
                    list(tool_args.keys()),
                    len(outcome),
                )

        # 达到 max_turns 仍未给出最终文本：再做一次无 tool 调用收尾
        result.truncated = True
        final = self.complete(system, "", messages=self._strip_system(convo, system))
        result.text = final or assistant_text
        self.logger.warning(
            "llm tool loop truncated reached_max_turns=%s tool_calls_total=%s",
            resolved_turns,
            len(result.tool_calls),
        )
        return result

    # ------------------------------------------------------------------ #
    # 原生 token 级流式
    # ------------------------------------------------------------------ #
    def stream(self, system: str, user: str, *, messages: list[Message] | None = None) -> Iterator[str]:
        if self._client is None:
            self.logger.info("llm stream skipped disabled")
            return
        msgs = self._normalize_messages(system, user, messages)
        self.logger.info(
            "llm stream start provider=%s model=%s messages=%s",
            self.settings.agent_provider,
            self.settings.agent_model,
            len(msgs),
        )
        if self.settings.agent_provider == "openai":
            try:
                stream = self._client.chat.completions.create(
                    model=self.settings.agent_model,
                    messages=msgs,
                    temperature=self.settings.agent_temperature,
                    stream=True,
                )
            except Exception:
                self.logger.exception("openai stream failed model=%s", self.settings.agent_model)
                raise
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        elif self.settings.agent_provider == "anthropic":
            system_text, convo = self._split_system(msgs)
            try:
                with self._client.messages.stream(
                    model=self.settings.agent_model,
                    max_tokens=self.settings.agent_max_tokens,
                    temperature=self.settings.agent_temperature,
                    system=system_text,
                    messages=convo,
                ) as stream:
                    yield from stream.text_stream
            except Exception:
                self.logger.exception("anthropic stream failed model=%s", self.settings.agent_model)
                raise
        else:
            raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")

    # ------------------------------------------------------------------ #
    # 内部：provider 适配
    # ------------------------------------------------------------------ #
    def _openai_text(self, msgs: list[Message]) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.settings.agent_model,
                messages=msgs,
                temperature=self.settings.agent_temperature,
            )
        except Exception:
            self.logger.exception("openai completion failed model=%s", self.settings.agent_model)
            raise
        return response.choices[0].message.content or ""

    def _anthropic_text(self, msgs: list[Message]) -> str:
        system_text, convo = self._split_system(msgs)
        try:
            response = self._client.messages.create(
                model=self.settings.agent_model,
                max_tokens=self.settings.agent_max_tokens,
                temperature=self.settings.agent_temperature,
                system=system_text,
                messages=convo,
            )
        except Exception:
            self.logger.exception("anthropic completion failed model=%s", self.settings.agent_model)
            raise
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    def _provider_tool_step(
        self, convo: list[Message], tools: list[ToolDef]
    ) -> tuple[list[dict[str, Any]], str]:
        """调一次 LLM，返回 (tool_calls 规范化列表, assistant 文本)。"""
        if self.settings.agent_provider == "openai":
            return self._openai_tool_step(convo, tools)
        if self.settings.agent_provider == "anthropic":
            return self._anthropic_tool_step(convo, tools)
        raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")

    def _openai_tool_step(self, convo, tools):
        payload_tools = [
            {"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.input_schema,
            }}
            for t in tools
        ]
        response = self._client.chat.completions.create(
            model=self.settings.agent_model,
            messages=convo,
            temperature=self.settings.agent_temperature,
            tools=payload_tools,
            tool_choice="auto",
        )
        msg = response.choices[0].message
        text = msg.content or ""
        calls: list[dict[str, Any]] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return calls, text

    def _anthropic_tool_step(self, convo, tools):
        system_text, split_convo = self._split_system(convo)
        payload_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        response = self._client.messages.create(
            model=self.settings.agent_model,
            max_tokens=self.settings.agent_max_tokens,
            temperature=self.settings.agent_temperature,
            system=system_text,
            messages=split_convo,
            tools=payload_tools,
        )
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
            elif getattr(block, "type", None) == "tool_use":
                calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input if isinstance(block.input, dict) else {},
                })
        return calls, "".join(text_parts)

    def _append_assistant_tool_step(
        self, convo: list[Message], text: str, calls: list[dict[str, Any]]
    ) -> None:
        if self.settings.agent_provider == "openai":
            convo.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                    for c in calls
                ],
            })
        else:  # anthropic
            content: list[dict[str, Any]] = []
            if text:
                content.append({"type": "text", "text": text})
            for c in calls:
                content.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]})
            convo.append({"role": "assistant", "content": content})

    def _append_tool_result(self, convo: list[Message], call: dict[str, Any], outcome: str) -> None:
        if self.settings.agent_provider == "openai":
            convo.append({"role": "tool", "tool_call_id": call["id"], "content": outcome})
        else:  # anthropic
            convo.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": outcome}
            ]})

    # ------------------------------------------------------------------ #
    # 内部：消息归一化
    # ------------------------------------------------------------------ #
    def _normalize_messages(
        self, system: str, user: str, messages: list[Message] | None
    ) -> list[Message]:
        if messages is not None:
            return messages
        msgs: list[Message] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        return msgs

    def _split_system(self, messages: list[Message]) -> tuple[str, list[Message]]:
        """把 system 角色消息提出来（anthropic 顶层参数），其余原样返回。"""
        sys_parts: list[str] = []
        rest: list[Message] = []
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content")
                if isinstance(content, str) and content:
                    sys_parts.append(content)
            else:
                rest.append(m)
        return "\n\n".join(sys_parts), rest

    def _strip_system(self, convo: list[Message], fallback_system: str) -> list[Message]:
        """把对话中的 system 消息整合到开头（用于 complete() 收尾）。"""
        system_text, rest = self._split_system(convo)
        head = system_text or fallback_system
        out: list[Message] = [{"role": "system", "content": head}] if head else []
        out.extend(rest)
        return out

    def _build_client(self):
        if not self.settings.agent_api_key:
            self.logger.info("llm client disabled missing api key provider=%s", self.settings.agent_provider)
            return None
        if self.settings.agent_provider == "openai":
            kwargs: dict[str, Any] = {"api_key": self.settings.agent_api_key}
            if self.settings.agent_base_url:
                kwargs["base_url"] = self.settings.agent_base_url
            return OpenAI(**kwargs)
        if self.settings.agent_provider == "anthropic":
            kwargs = {"api_key": self.settings.agent_api_key}
            if self.settings.agent_base_url:
                kwargs["base_url"] = self.settings.agent_base_url
            return Anthropic(**kwargs)
        raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")


def format_sources(sources: list[SourceChunk]) -> str:
    if not sources:
        return "无可用知识库片段。"
    return "\n\n".join(
        f"[{i + 1}] {s.path} score={s.score}\n{s.text}" for i, s in enumerate(sources)
    )
