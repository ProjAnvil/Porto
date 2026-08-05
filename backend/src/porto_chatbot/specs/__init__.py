"""子系统规格的生成与 evaluator-optimizer loop。

固定 workflow 的 generate_specs 节点内部，对每个子系统运行：
    generate → critique → refine → critique → …
直到 PASS / max_iter / 分数不升 / 预算上限（四重终止）。

依据：Anthropic Evaluator-Optimizer workflow + Self-Refine。
官方示例缺 max-iter guard，本实现显式补齐四重终止。

公共 API：保持与原 ``specs.py`` 单文件模块向后兼容，
``from porto_chatbot.specs import X`` 对所有调用方仍然可用。
"""
from __future__ import annotations

from .context import SpecContext
from .loop import generate_spec_with_loop
from .rubric import SPEC_RUBRIC
from .steps import critique_spec, generate_initial_spec, refine_spec
from .subgraph import SpecSubgraphState, build_spec_subgraph
from .template import render_template_spec

__all__ = [
    "SPEC_RUBRIC",
    "SpecContext",
    "SpecSubgraphState",
    "build_spec_subgraph",
    "render_template_spec",
    "generate_initial_spec",
    "critique_spec",
    "refine_spec",
    "generate_spec_with_loop",
]
