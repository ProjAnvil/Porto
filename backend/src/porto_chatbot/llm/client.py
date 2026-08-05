# complete_document 保留原生 SDK:U4 未验证 langchain 多模态 PDF(设计 D10)
from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from typing import Any

from anthropic import Anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import OpenAI

from ..logging_utils import get_component_logger
from ..models.enums import ChatRole, LLMProvider, TruncationReason
from ..settings import Settings
from .parsing import _try_parse_json
from .types import (
    ContentType,
    FinishReason,
    Message,
    ModelCapabilities,
    ToolCall,
    ToolDef,
    ToolLoopResult,
)

_CONTINUATION_TAIL_CHARS = 200


def _finish_reason(response) -> str | None:
    """从 langchain AIMessage.response_metadata 取归一化的 finish_reason。

    OpenAI provider 字段为 ``finish_reason``;Anthropic 为 ``stop_reason``。归一化:
    Anthropic 的 ``"max_tokens"`` 映射为 OpenAI 语义的 ``"length"``,使上层检测分支
    对两家 provider 通用。返回 None = 字段缺失/异常(视为非 length,走正常分支)。
    """
    meta = getattr(response, "response_metadata", None)
    if not isinstance(meta, dict):
        return None
    fr = meta.get("finish_reason") or meta.get("stop_reason")
    if fr == "max_tokens":  # Anthropic 语义 → 归一化为 OpenAI "length"
        return FinishReason.LENGTH.value
    return fr


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = get_component_logger("llm", settings)
        self._client = self._build_client()
        self._native_client = self._build_native_client()
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

    @property
    def document_capabilities(self) -> ModelCapabilities:
        if not self.enabled:
            return ModelCapabilities(False, False, False, "LLM client is disabled")
        model = self.settings.agent_model.lower()
        if self.settings.agent_provider == LLMProvider.ANTHROPIC:
            supported = model.startswith("claude-")
            return ModelCapabilities(True, supported, supported, "Anthropic model family")
        supported = bool(re.match(r"^(gpt-(?:4o|4\.1|5(?:\.|-|$))|o[134](?:-|$))", model))
        reason = "known OpenAI vision model family" if supported else "unknown model capability"
        return ModelCapabilities(True, supported, supported, reason)

    def complete_document(
        self, filename: str, data: bytes, media_type: str, prompt: str
    ) -> str | None:
        """Analyze one PDF with the provider's native document input."""
        if not self.document_capabilities.native_pdf:
            return None
        if self._native_client is None:
            return None
        encoded = base64.standard_b64encode(data).decode("ascii")
        if self.settings.agent_provider == LLMProvider.OPENAI:
            response = self._native_client.chat.completions.create(
                model=self.settings.agent_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "file",
                                "file": {
                                    "filename": filename,
                                    "file_data": f"data:{media_type};base64,{encoded}",
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                temperature=self.settings.agent_temperature,
            )
            return response.choices[0].message.content or ""
        response = self._native_client.messages.create(
            model=self.settings.agent_model,
            max_tokens=self.settings.document_max_tokens,
            temperature=self.settings.agent_temperature,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == ContentType.TEXT
        )

    # ------------------------------------------------------------------ #
    # 基础补全：兼容旧 complete(system, user)，新增 messages 多轮
    # ------------------------------------------------------------------ #
    def complete(
        self, system: str, user: str, *, messages: list[Message] | None = None
    ) -> str | None:
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
        try:
            response = self._client.invoke(self._to_lc_messages(msgs))
        except Exception:
            self.logger.exception("llm complete failed model=%s", self.settings.agent_model)
            raise
        content = response.content
        if not isinstance(content, str):
            # 多模态/tool 回包：取文本块拼接
            content = "".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
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
        self.logger.warning(
            "llm complete_structured parse failed retrying raw_chars=%s", len(raw or "")
        )
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

        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]
        bound = self._client.bind_tools(tool_specs)
        handlers = {t.name: t for t in tools}
        resolved_turns = max_turns or self.settings.agent_max_tool_turns
        result = ToolLoopResult()

        convo = self._to_lc_messages(self._normalize_messages(system, user, messages))
        # 当前轮输出 token 上限:撞 finish_reason="length" 时升级(×4),整个 loop 仅升级一次。
        # 通过 invoke(max_tokens=...) kwarg 覆盖(langgraph _get_request_payload: {**default, **kwargs})。
        cur_max_tokens = self.settings.agent_max_tokens
        escalated = False

        for turn in range(1, resolved_turns + 1):
            result.turns = turn
            response = bound.invoke(convo, max_tokens=cur_max_tokens)
            tool_calls = response.tool_calls or []
            assistant_text = response.content if isinstance(response.content, str) else ""
            finish_reason = _finish_reason(response)

            # ── 检测层:单次回复被 max_tokens 硬切(finish_reason="length")──
            # 此时本轮输出(可能含 tool_call)不完整、不可直接使用(Anthropic 官方)。
            if finish_reason == FinishReason.LENGTH:
                if not escalated:
                    # 第 1 层·升级:丢弃本轮残缺输出,用更大 max_tokens 重发同一 convo(不执行残帧)
                    escalated = True
                    cur_max_tokens = self.settings.agent_max_tokens * 4
                    self.logger.warning(
                        "llm tool loop max_tokens truncated, escalating to %s",
                        cur_max_tokens,
                    )
                    continue
                # 升级后仍 length
                if not tool_calls:
                    # 第 2 层·续写:收敛输出被切 → 注入「请继续」续写拼接(火山方案)
                    text, recovered = self._continue_after_length(
                        bound, convo, response, cur_max_tokens
                    )
                    if recovered:
                        result.text = text
                        self.logger.info(
                            "llm tool loop recovered via continue final_chars=%s",
                            len(text),
                        )
                        return result
                # 第 3 层·兜底:续写不收敛 / tool_call 仍被切 → 截断标记,走节点固定提示 + 重跑
                result.truncated = True
                result.text = ""  # 残缺产出不暴露
                result.reason = TruncationReason.MAX_TOKENS_TRUNCATED
                self.logger.warning(
                    "llm tool loop still truncated after escalate+continue "
                    "max_tokens=%s tool_calls=%s",
                    cur_max_tokens, len(tool_calls),
                )
                return result

            # ── 正常分支 ──
            if not tool_calls:
                result.text = assistant_text
                self.logger.info(
                    "llm tool loop stop reason=no_tool_calls turns=%s total=%s",
                    turn, len(result.tool_calls),
                )
                return result

            convo.append(response)  # AIMessage(含 tool_calls)
            for tc in tool_calls:
                name = tc["name"]
                args = tc.get("args") or {}
                tool_def = handlers.get(name)
                if tool_def is None:
                    outcome = f"错误：未知工具 {name}"
                else:
                    try:
                        outcome = tool_def.handler(args)
                    except Exception as exc:  # noqa: BLE001
                        self.logger.exception("llm tool handler failed name=%s", name)
                        outcome = f"错误：工具 {name} 执行失败：{exc}"
                result.tool_calls.append(ToolCall(name=name, arguments=args, result=outcome))
                convo.append(ToolMessage(content=outcome, tool_call_id=tc["id"]))
                self.logger.info(
                    "llm tool call name=%s args_keys=%s result_chars=%s",
                    name, list(args.keys()), len(outcome),
                )

        # tool-turn 用尽仍有 tool_calls(plan 原治理)
        result.truncated = True
        result.text = ""  # 截断 = 无可靠产出;过渡语清空,不暴露给前端
        result.reason = TruncationReason.TOOL_LOOP_TRUNCATED
        self.logger.warning(
            "llm tool loop truncated max_turns=%s total=%s",
            resolved_turns, len(result.tool_calls),
        )
        return result

    def _continue_after_length(
        self, bound, convo, partial_response, cur_max_tokens
    ) -> tuple[str, bool]:
        """升级后仍 ``finish_reason="length"``:注入「请继续」续写,多段拼接(火山方案)。

        把部分输出作 assistant 消息进历史,再追加「请继续刚才的回答,不要重复内容:\\n{尾部
        200 字}」user 消息,让模型基于自己已写的内容续写。最多 ``max_output_recovery_attempts``
        轮。返回 ``(full_text, recovered)``;``recovered=False`` 表示轮数用尽仍 length。
        """
        max_rounds = self.settings.max_output_recovery_attempts
        full = partial_response.content if isinstance(partial_response.content, str) else ""
        # 部分输出进历史(assistant),模型基于完整上下文(含自己已写)续写
        cont = list(convo) + [partial_response]
        for _ in range(max_rounds):
            tail = full[-_CONTINUATION_TAIL_CHARS:]
            cont = cont + [
                HumanMessage(content=f"请继续刚才的回答，不要重复内容：\n{tail}")
            ]
            resp = bound.invoke(cont, max_tokens=cur_max_tokens)
            chunk = resp.content if isinstance(resp.content, str) else ""
            full += chunk
            if _finish_reason(resp) != FinishReason.LENGTH:
                return full, True
            cont = cont + [resp]  # 下一轮续写:把这次的部分输出也并入历史
        return full, False

    # ------------------------------------------------------------------ #
    # 原生 token 级流式
    # ------------------------------------------------------------------ #
    def stream(
        self, system: str, user: str, *, messages: list[Message] | None = None
    ) -> Iterator[str]:
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
        try:
            for chunk in self._client.stream(self._to_lc_messages(msgs)):
                delta = chunk.content
                if isinstance(delta, str) and delta:
                    yield delta
        except Exception:
            self.logger.exception("llm stream failed model=%s", self.settings.agent_model)
            raise

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

    def _to_lc_messages(self, msgs: list[Message]) -> list:
        """把 openai 风格 role/content dict 转为 langchain BaseMessage。"""
        out = []
        for m in msgs:
            role = m.get("role")
            content = m.get("content")
            if role == ChatRole.SYSTEM:
                out.append(SystemMessage(content=content))
            elif role == ChatRole.USER:
                out.append(HumanMessage(content=content))
            elif role == ChatRole.ASSISTANT:
                out.append(AIMessage(content=content))
            else:
                # 未知角色兜底为 user 消息（tool 结果由 complete_with_tools 用 ToolMessage 单独处理）
                out.append(HumanMessage(content=content))
        return out

    def _build_client(self) -> BaseChatModel | None:
        if not self.settings.agent_api_key:
            self.logger.info(
                "llm client disabled missing api key provider=%s", self.settings.agent_provider
            )
            return None
        kwargs: dict[str, Any] = {
            "api_key": self.settings.agent_api_key,
            "model": self.settings.agent_model,
            "temperature": self.settings.agent_temperature,
            "max_tokens": self.settings.agent_max_tokens,
            "timeout": self.settings.agent_request_timeout,
        }
        if self.settings.agent_base_url:
            kwargs["base_url"] = self.settings.agent_base_url
        if self.settings.agent_provider == LLMProvider.OPENAI:
            client: BaseChatModel = ChatOpenAI(**kwargs)
        elif self.settings.agent_provider == LLMProvider.ANTHROPIC:
            client = ChatAnthropic(**kwargs)
        else:
            raise ValueError(f"Unsupported agent provider: {self.settings.agent_provider}")
        return client

    def _build_native_client(self):
        """原生 SDK client(供 complete_document 使用,U4 未验证 langchain 多模态 PDF)。"""
        if not self.settings.agent_api_key:
            return None
        kwargs = {
            "api_key": self.settings.agent_api_key,
            "timeout": self.settings.agent_request_timeout,
        }
        if self.settings.agent_base_url:
            kwargs["base_url"] = self.settings.agent_base_url
        if self.settings.agent_provider == LLMProvider.OPENAI:
            return OpenAI(**kwargs)
        if self.settings.agent_provider == LLMProvider.ANTHROPIC:
            return Anthropic(**kwargs)
        return None
