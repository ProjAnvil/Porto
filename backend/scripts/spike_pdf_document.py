"""Spike: 验证 ChatOpenAI/ChatAnthropic 能否处理 provider 特定 PDF document。

手动运行(需 LANGCHAIN_API_KEY + 一个 PDF 文件):
    cd backend && python -m scripts.spike_pdf_document <pdf> | openai|anthropic

结论填回本 plan §Spike Conclusions 与设计文档 §11 U4。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from porto_chatbot.settings import settings


def main(pdf: str, provider: str) -> None:
    data = Path(pdf).read_bytes()
    encoded = base64.standard_b64encode(data).decode("ascii")
    prompt = "用中文摘要这份文档的核心需求。"
    model_name = settings.agent_model

    if provider == "openai":
        model = ChatOpenAI(model=model_name, api_key=settings.agent_api_key, base_url=settings.agent_base_url)
        msg = HumanMessage(content=[
            {"type": "file", "file": {"filename": Path(pdf).name, "file_data": f"data:application/pdf;base64,{encoded}"}},
            {"type": "text", "text": prompt},
        ])
    else:
        model = ChatAnthropic(model=model_name, api_key=settings.agent_api_key, base_url=settings.agent_base_url)
        msg = HumanMessage(content=[
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}},
            {"type": "text", "text": prompt},
        ])

    resp = model.invoke([msg])
    print(type(resp.content), resp.content if isinstance(resp.content, str) else "(multimodal content)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
