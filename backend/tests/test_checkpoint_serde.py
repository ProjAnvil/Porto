"""F1: the langgraph SqliteSaver serde registers the Pydantic types of porto_chatbot.models.*.

Without the registration, Pydantic models going through checkpoint serialization emit a
deprecation warning ("Deserializing unregistered type ..."). Today it is only a warning; a
future langgraph version will block hard (LANGGRAPH_STRICT_MSGPACK=true blocks now).
With the registration the round-trip emits no warning.
"""

from __future__ import annotations

import warnings

from porto_chatbot.api.deps import _build_checkpoint_serde
from porto_chatbot.models.common import SourceChunk
from porto_chatbot.models.spec import SpecAttempt, SpecResult
from porto_chatbot.models.workflow import AgentStep, Subsystem

#: All porto Pydantic models that travel through the workflow graph state across a
#: checkpoint round-trip.
_PORTO_MODELS = (AgentStep, Subsystem, SourceChunk, SpecAttempt, SpecResult)


def test_checkpoint_serde_registers_porto_models():
    """F1: the checkpointer serde must add the Pydantic types of porto_chatbot.models.* to
    allowed_msgpack_modules (a white-box assertion of the configuration contract: the warning
    mechanism is not warnings.warn, so catch_warnings cannot capture it, hence asserting on
    the registration list directly)."""
    serde = _build_checkpoint_serde()
    allowed = serde._allowed_msgpack_modules
    for cls in _PORTO_MODELS:
        assert (cls.__module__, cls.__name__) in allowed, (
            f"{cls.__module__}.{cls.__name__} 未注册到 checkpoint serde"
        )


def test_checkpoint_serde_roundtrips_porto_models():
    """F1: after the registration the serde round-trips 5 Pydantic models and they are still
    instances of the same type (spike 4 behavior unchanged)."""
    serde = _build_checkpoint_serde()
    samples = [
        AgentStep(name="retrieve", status="completed", summary="ok"),
        Subsystem(name="pay", responsibility="支付授权"),
        SourceChunk(id="c1", path="a.md", title="A", text="正文"),
        SpecAttempt(version=1, score=10, verdict="PASS"),
        SpecResult(final="spec body", iterations=2),
    ]
    # The registration does not affect serialization correctness; simplefilter("ignore") only
    # silences occasional warnings from the msgpack C layer.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for obj in samples:
            blob = serde.dumps_typed(obj)
            restored = serde.loads_typed(blob)
            assert type(restored) is type(obj), (
                f"{type(obj).__name__} 往返后变 {type(restored).__name__}"
            )
