"""F1: langgraph SqliteSaver 的 serde 注册 porto_chatbot.models.* 的 Pydantic 类型。

未注册时,Pydantic 模型过 checkpoint 序列化会发 deprecation warning
("Deserializing unregistered type ...")——当前仅警告,未来 langgraph 版本会
硬阻断(LANGGRAPH_STRICT_MSGPACK=true 即可现在阻断)。注册后往返不发 warning。
"""

from __future__ import annotations

import warnings

from porto_chatbot.api.deps import _build_checkpoint_serde
from porto_chatbot.models.common import SourceChunk
from porto_chatbot.models.spec import SpecAttempt, SpecResult
from porto_chatbot.models.workflow import AgentStep, Subsystem

#: workflow graph state 经 checkpoint 往返的所有 porto Pydantic 模型。
_PORTO_MODELS = (AgentStep, Subsystem, SourceChunk, SpecAttempt, SpecResult)


def test_checkpoint_serde_registers_porto_models():
    """F1: checkpointer serde 必须把 porto_chatbot.models.* 的 Pydantic 类型
    加入 allowed_msgpack_modules(白盒断言配置契约 —— warning 机制非 warnings.warn,
    catch_warnings 抓不到,故直接断言注册列表)。"""
    serde = _build_checkpoint_serde()
    allowed = serde._allowed_msgpack_modules
    for cls in _PORTO_MODELS:
        assert (cls.__module__, cls.__name__) in allowed, (
            f"{cls.__module__}.{cls.__name__} 未注册到 checkpoint serde"
        )


def test_checkpoint_serde_roundtrips_porto_models():
    """F1: 注册后的 serde 往返 5 个 Pydantic 模型,仍是同类型实例(spike ④ 行为不变)。"""
    serde = _build_checkpoint_serde()
    samples = [
        AgentStep(name="retrieve", status="completed", summary="ok"),
        Subsystem(name="pay", responsibility="支付授权"),
        SourceChunk(id="c1", path="a.md", title="A", text="正文"),
        SpecAttempt(version=1, score=10, verdict="PASS"),
        SpecResult(final="spec body", iterations=2),
    ]
    # 注册不影响序列化正确性;simplefilter ignore 仅屏蔽或msgpack C 层偶发告警。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for obj in samples:
            blob = serde.dumps_typed(obj)
            restored = serde.loads_typed(blob)
            assert type(restored) is type(obj), (
                f"{type(obj).__name__} 往返后变 {type(restored).__name__}"
            )
