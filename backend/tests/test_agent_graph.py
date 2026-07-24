"""agent graph:state reducer + 拓扑 + interrupt + update_state。"""
from __future__ import annotations

import operator
from typing import get_type_hints

from porto_chatbot.agent.state import PortoAgentState, _dict_merge


def test_dict_merge_reducer():
    assert _dict_merge(None, {"a": 1}) == {"a": 1}
    assert _dict_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert _dict_merge({"a": 1, "x": 9}, {"a": 2}) == {"a": 2, "x": 9}  # 右覆盖 + 保旧


def test_state_reducer_annotations():
    """steps→operator.add(append);specs/spec_results→_dict_merge;current_step 存在。"""
    th = get_type_hints(PortoAgentState, include_extras=True)
    assert operator.add in th["steps"].__metadata__
    assert _dict_merge in th["specs"].__metadata__
    assert _dict_merge in th["spec_results"].__metadata__
    assert "current_step" in th
